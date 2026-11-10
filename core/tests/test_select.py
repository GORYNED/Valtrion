import contextlib
import copy
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from valtrion import cli, constants, report, select

CORE_ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = CORE_ROOT.parent / "contracts"
FIXTURES = CONTRACTS / "fixtures" / "valid"
RUNNER = "python tools/run-tests.py --assemblies {assemblies}"
PLAN_KEY = "example.perf.open-playback"  # id inside fixtures/valid/plan.json

CHECKS = {
    "schema": "gdv.checks/1",
    "platforms": {"required": ["ios"], "dev": ["editor"]},
    "unityEditorSlots": 1,
    "areas": [
        {"glob": "src/media/**", "kind": "media"},
        {"glob": "src/ui/**", "kind": "ui"},
        {"glob": "src/shared/*.cs", "kind": "media"},
        {"glob": "src/shared/*.cs", "kind": "ui"},
        {"glob": "docs/**", "kind": "docs"},
        {"glob": "validation/**", "kind": "docs"},
    ],
    "kinds": {
        "media": {
            "L0": ["l0"],
            "L2": [f"editmode:Media.Tests,Media.More.Tests@{RUNNER}", f"playmode:Media.Play.Tests@{RUNNER}"],
            "L3": ["player:win64"],
            "L4": ["device:plans/open-playback"],
        },
        "ui": {"L1": ["l0:lint"], "L2": [f"editmode:Ui.Tests@{RUNNER}", f"editmode:Media.Tests@{RUNNER}"]},
        "docs": {"L0": ["l0"]},
    },
    "unclassified": "widest",
}


def git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", "-C", str(root), *arguments],
        capture_output=True, text=True, check=True,
    )
    return completed.stdout.strip()


def write(root: Path, relative: str, text: str = "x\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class RepoCase(unittest.TestCase):
    """A temporary git repository with a base commit on `main` and a `work` branch checked out."""

    checks: dict = CHECKS

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        git(self.root, "init", "-q", "-b", "main")
        for relative in ("src/media/a.cs", "src/media/old.cs", "src/ui/b.cs", "src/shared/s.cs", "docs/readme.md", "other/x.txt"):
            write(self.root, relative)
        self.checks_path = write(self.root, "validation/device/checks.json", json.dumps(self.checks))
        plan = json.loads((FIXTURES / "plan.json").read_text(encoding="utf-8"))
        write(self.root, "validation/device/plans/open-playback.plan.json", json.dumps(plan))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        git(self.root, "checkout", "-q", "-b", "work")

    def commit(self, message: str = "change") -> str:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", message)
        return git(self.root, "rev-parse", "HEAD")

    def config(self) -> select.Config:
        return select.load_config(self.checks_path)

    def selection(self, level: str = "L4", include_worktree: bool = False) -> select.Selection:
        paths, merge_base = select.changed_paths(self.root, "main", include_worktree)
        return select.select(self.config(), paths, level, base_ref="main", merge_base=merge_base, include_worktree=include_worktree)


class GlobTests(unittest.TestCase):
    def matches(self, glob: str, path: str) -> bool:
        return bool(select.glob_to_regex(glob).match(path))

    def test_double_star_spans_folders_and_single_star_does_not(self):
        self.assertTrue(self.matches("a/**", "a/b/c.cs"))
        self.assertTrue(self.matches("**/c.cs", "c.cs"))
        self.assertTrue(self.matches("**/c.cs", "a/b/c.cs"))
        self.assertTrue(self.matches("a/*.cs", "a/c.cs"))
        self.assertFalse(self.matches("a/*.cs", "a/b/c.cs"))
        self.assertFalse(self.matches("a/**", "ab/c.cs"))
        self.assertTrue(self.matches("a/?.cs", "a/c.cs"))
        self.assertFalse(self.matches("a/?.cs", "a/cc.cs"))

    def test_special_characters_are_literal(self):
        self.assertTrue(self.matches("Assets/Project (1)/**", "Assets/Project (1)/x.cs"))
        self.assertFalse(self.matches("a.b", "aXb"))
        self.assertTrue(self.matches("tools/Sample Project/**", "tools/Sample Project/Assets/x.cs"))


class SpecTests(unittest.TestCase):
    def test_valid_entries(self):
        spec = select.parse_spec("l0")
        self.assertEqual(("l0", "", None), (spec.type, spec.target, spec.runner))
        spec = select.parse_spec("editmode:A.Tests, B.Tests@python r.py --assemblies {assemblies}")
        self.assertEqual(("editmode", "A.Tests,B.Tests", "python r.py --assemblies {assemblies}"), (spec.type, spec.target, spec.runner))
        self.assertEqual("plans/x", select.parse_spec("device:plans/x").target)

    def test_invalid_entries(self):
        for raw in ("unknown:x", "editmode", "editmode:", "editmode:bad name", "device:../x", "device:/x", "player:x@runner", "l0@x", "editmode:A@"):
            with self.subTest(raw), self.assertRaises(ValueError):
                select.parse_spec(raw)


class SelectionTests(RepoCase):
    def test_union_is_cumulative_per_level(self):
        write(self.root, "src/media/a.cs", "changed\n")
        write(self.root, "src/ui/b.cs", "changed\n")
        self.commit()
        expected = {
            "L0": ["l0"],
            "L1": ["l0", "l0:lint"],
            "L2": ["editmode:Media.More.Tests", "editmode:Media.Tests", "editmode:Ui.Tests", "l0", "l0:lint", "playmode:Media.Play.Tests"],
            "L3": ["editmode:Media.More.Tests", "editmode:Media.Tests", "editmode:Ui.Tests", "l0", "l0:lint", "playmode:Media.Play.Tests", "player:win64"],
            "L4": ["editmode:Media.More.Tests", "editmode:Media.Tests", "editmode:Ui.Tests", "l0", "l0:lint", PLAN_KEY, "playmode:Media.Play.Tests", "player:win64"],
        }
        for level, keys in expected.items():
            with self.subTest(level):
                self.assertEqual(sorted(keys), sorted(check.key for check in self.selection(level).checks))

    def test_checks_carry_the_lowest_level_and_every_kind(self):
        write(self.root, "src/media/a.cs", "changed\n")
        write(self.root, "src/ui/b.cs", "changed\n")
        self.commit()
        checks = {check.key: check for check in self.selection().checks}
        self.assertEqual("L2", checks["editmode:Media.Tests"].level)
        self.assertEqual({"media", "ui"}, checks["editmode:Media.Tests"].kinds)
        self.assertEqual({"media"}, checks["player:win64"].kinds)
        self.assertEqual("L4", checks[PLAN_KEY].level)

    def test_unrelated_kinds_are_not_selected(self):
        write(self.root, "docs/readme.md", "changed\n")
        self.commit()
        selection = self.selection()
        self.assertEqual(["l0"], [check.key for check in selection.checks])
        self.assertFalse(selection.widest)
        self.assertEqual(["docs"], selection.kinds)

    def test_a_path_in_several_areas_selects_all_their_kinds(self):
        write(self.root, "src/shared/s.cs", "changed\n")
        self.commit()
        self.assertEqual(["media", "ui"], self.selection().kinds)

    def test_unclassified_path_selects_the_widest_set(self):
        write(self.root, "other/x.txt", "changed\n")
        self.commit()
        selection = self.selection()
        self.assertTrue(selection.widest)
        self.assertEqual(["other/x.txt"], selection.unclassified)
        self.assertEqual(["docs", "media", "ui"], selection.kinds)
        self.assertIn(PLAN_KEY, [check.key for check in selection.checks])
        self.assertIn("editmode:Ui.Tests", [check.key for check in selection.checks])
        self.assertEqual([], [check.key for check in self.selection("L1").checks if check.type == "device"])

    def test_unclassified_path_with_the_fail_policy_selects_nothing(self):
        self.checks_path.write_text(json.dumps({**CHECKS, "unclassified": "fail"}), encoding="utf-8")
        write(self.root, "other/x.txt", "changed\n")
        write(self.root, "docs/readme.md", "changed\n")
        self.commit()
        selection = self.selection()
        self.assertTrue(selection.failed)
        self.assertEqual([], selection.checks)
        code, _, err = run("select", "--level", "L4", "--base-ref", "main", "--checks", str(self.checks_path))
        self.assertEqual(1, code)
        self.assertIn("other/x.txt", err + _)

    def test_deleted_and_renamed_paths_are_classified_by_their_old_and_new_location(self):
        (self.root / "src" / "media" / "a.cs").unlink()
        (self.root / "src" / "media" / "old.cs").rename(self.root / "docs" / "moved.md")
        self.commit()
        selection = self.selection()
        self.assertEqual(["docs/moved.md", "src/media/a.cs", "src/media/old.cs"], selection.paths)
        self.assertEqual({"docs", "media"}, set(selection.kinds))
        self.assertEqual([], selection.unclassified)

    def test_empty_diff_selects_nothing(self):
        selection = self.selection()
        self.assertEqual([], selection.paths)
        self.assertEqual([], selection.checks)
        code, out, _ = run("select", "--level", "L4", "--base-ref", "main", "--checks", str(self.checks_path))
        self.assertEqual(0, code)
        self.assertIn("Nothing selected", out)

    def test_worktree_changes_count_only_with_the_flag(self):
        write(self.root, "src/ui/b.cs", "dirty\n")  # unstaged
        write(self.root, "docs/new.md", "untracked\n")
        self.assertEqual([], self.selection().paths)
        self.assertEqual(["docs/new.md", "src/ui/b.cs"], self.selection(include_worktree=True).paths)
        git(self.root, "add", "docs/new.md")  # staged
        self.assertEqual(["docs/new.md", "src/ui/b.cs"], self.selection(include_worktree=True).paths)

    def test_worktree_diff_includes_committed_changes_once(self):
        write(self.root, "src/ui/b.cs", "committed\n")
        self.commit()
        write(self.root, "src/ui/b.cs", "dirty\n")
        self.assertEqual(["src/ui/b.cs"], self.selection(include_worktree=True).paths)

    def test_the_merge_base_is_used_when_the_base_moved_on(self):
        write(self.root, "docs/readme.md", "mine\n")
        self.commit()
        git(self.root, "checkout", "-q", "main")
        write(self.root, "other/x.txt", "base moved\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "main moved")
        git(self.root, "checkout", "-q", "work")
        self.assertEqual(["docs/readme.md"], self.selection().paths)

    def test_editor_suites_share_one_command_per_runner(self):
        write(self.root, "src/media/a.cs", "changed\n")
        write(self.root, "src/ui/b.cs", "changed\n")
        self.commit()
        commands = self.selection("L2").editor_commands()
        self.assertEqual(1, len(commands))
        self.assertEqual(
            "python tools/run-tests.py --assemblies Media.More.Tests,Media.Play.Tests,Media.Tests,Ui.Tests", commands[0]["command"]
        )

    def test_a_suite_without_a_runner_has_no_command(self):
        self.checks_path.write_text(json.dumps({**CHECKS, "kinds": {**CHECKS["kinds"], "docs": {"L2": ["editmode:Docs.Tests"]}}}), encoding="utf-8")
        write(self.root, "docs/readme.md", "changed\n")
        self.commit()
        commands = self.selection("L2").editor_commands()
        self.assertEqual([None], [entry["command"] for entry in commands])
        self.assertEqual(["Docs.Tests"], commands[0]["assemblies"])

    def test_device_plans_carry_platforms_capabilities_and_evidence(self):
        write(self.root, "src/media/a.cs", "changed\n")
        self.commit()
        selection = self.selection()
        payload = selection.to_json()
        plan = payload["devicePlans"][0]
        self.assertEqual(PLAN_KEY, plan["key"])
        self.assertEqual(["ios"], plan["platforms"])
        self.assertEqual(["camera.120fps"], plan["capabilities"])
        self.assertEqual(["physical"], plan["evidence"])
        self.assertTrue(plan["requiresPhysicalDevice"])
        self.assertEqual(["ios"], payload["requiredPlatforms"])
        self.assertEqual([PLAN_KEY], payload["afterFinalReview"])
        self.assertEqual({"required": ["ios"], "dev": ["editor"]}, payload["projectPlatforms"])

    def test_output_is_deterministic(self):
        write(self.root, "src/media/a.cs", "changed\n")
        write(self.root, "src/ui/b.cs", "changed\n")
        write(self.root, "other/x.txt", "changed\n")
        self.commit()
        outputs = {json.dumps(self.selection().to_json()) for _ in range(3)}
        self.assertEqual(1, len(outputs))
        first = run("select", "--level", "L4", "--base-ref", "main", "--checks", str(self.checks_path), "--json")
        second = run("select", "--level", "L4", "--base-ref", "main", "--checks", str(self.checks_path), "--json")
        self.assertEqual(first, second)

    def test_cli_json_and_text(self):
        write(self.root, "src/media/a.cs", "changed\n")
        self.commit()
        code, out, _ = run("select", "--base-ref", "main", "--checks", str(self.checks_path), "--level", "L2", "--json")
        self.assertEqual(0, code)
        payload = json.loads(out)
        self.assertEqual("L2", payload["level"])
        self.assertEqual([], payload["devicePlans"])
        self.assertEqual(["Media.More.Tests", "Media.Play.Tests", "Media.Tests"], payload["editorCommands"][0]["assemblies"])
        code, out, _ = run("select", "--level", "L4", "--base-ref", "main", "--checks", str(self.checks_path))
        self.assertEqual(0, code)
        self.assertIn("Editor suites:", out)
        self.assertIn("Device plans", out)
        self.assertIn("Required platforms: ios", out)

    def test_unknown_base_ref_is_blocked(self):
        code, _, err = run("select", "--base-ref", "no-such-ref", "--checks", str(self.checks_path))
        self.assertEqual(2, code)
        self.assertIn("BLOCKED", err)

    def test_missing_checks_file_is_blocked_and_invalid_content_fails(self):
        code, _, err = run("select", "--base-ref", "main", "--checks", str(self.root / "validation" / "nope.json"))
        self.assertEqual(2, code)
        self.assertIn("BLOCKED", err)
        self.checks_path.write_text(json.dumps({**CHECKS, "unclassified": "maybe"}), encoding="utf-8")
        code, _, err = run("select", "--level", "L4", "--base-ref", "main", "--checks", str(self.checks_path))
        self.assertEqual(1, code)
        self.assertIn("unclassified", err)

    def test_a_broken_plan_fails_selection(self):
        write(self.root, "src/media/a.cs", "changed\n")
        self.commit()
        (self.root / "validation" / "device" / "plans" / "open-playback.plan.json").write_text("{}", encoding="utf-8")
        code, _, err = run("select", "--level", "L4", "--base-ref", "main", "--checks", str(self.checks_path))
        self.assertEqual(1, code)
        self.assertIn("open-playback", err)


class CheckConfigTests(RepoCase):
    def test_valid_configuration(self):
        code, out, _ = run("check-config", "--checks", str(self.checks_path))
        self.assertEqual(0, code)
        self.assertIn("valid gdv.checks/1", out)

    def test_area_with_an_unknown_kind(self):
        bad = copy.deepcopy(CHECKS)
        bad["areas"].append({"glob": "x/**", "kind": "ghost"})
        self.checks_path.write_text(json.dumps(bad), encoding="utf-8")
        code, _, err = run("check-config", "--checks", str(self.checks_path))
        self.assertEqual(1, code)
        self.assertIn("ghost", err)

    def test_missing_plan_bad_entry_and_duplicate_plan_ids(self):
        bad = copy.deepcopy(CHECKS)
        bad["kinds"]["docs"]["L4"] = ["device:plans/missing", "bogus:thing"]
        self.checks_path.write_text(json.dumps(bad), encoding="utf-8")
        code, _, err = run("check-config", "--checks", str(self.checks_path))
        self.assertEqual(1, code)
        self.assertIn("missing", err)
        self.assertIn("bogus", err)
        write(self.root, "validation/device/plans/copy.plan.json", (self.root / "validation/device/plans/open-playback.plan.json").read_text(encoding="utf-8"))
        bad["kinds"]["docs"]["L4"] = ["device:plans/copy"]
        self.checks_path.write_text(json.dumps(bad), encoding="utf-8")
        code, _, err = run("check-config", "--checks", str(self.checks_path))
        self.assertEqual(1, code)
        self.assertIn("is used by", err)

    def test_invalid_plan_file_is_an_error(self):
        (self.root / "validation" / "device" / "plans" / "open-playback.plan.json").write_text('{"schema": "gdv.plan/1"}', encoding="utf-8")
        code, _, err = run("check-config", "--checks", str(self.checks_path))
        self.assertEqual(1, code)
        self.assertIn("open-playback", err)

    def test_unused_kind_is_only_a_warning(self):
        extra = copy.deepcopy(CHECKS)
        extra["kinds"]["spare"] = {"L2": ["l0"]}
        self.checks_path.write_text(json.dumps(extra), encoding="utf-8")
        code, _, err = run("check-config", "--checks", str(self.checks_path))
        self.assertEqual(0, code)
        self.assertIn("warning", err)
        self.assertIn("spare", err)

    def test_unclassified_paths_are_errors_or_warnings(self):
        write(self.root, "other/x.txt", "changed\n")
        write(self.root, "docs/readme.md", "changed\n")
        self.commit()
        code, _, err = run("check-config", "--checks", str(self.checks_path), "--base-ref", "main")
        self.assertEqual(1, code)
        self.assertIn("other/x.txt", err)
        self.assertNotIn("docs/readme.md", err)
        code, _, err = run("check-config", "--checks", str(self.checks_path), "--base-ref", "main", "--warn-unclassified")
        self.assertEqual(0, code)
        self.assertIn("warning", err)
        self.assertIn("other/x.txt", err)

    def test_unreadable_or_missing_file_is_blocked(self):
        code, _, err = run("check-config", "--checks", str(self.root / "absent.json"))
        self.assertEqual(2, code)
        self.assertIn("BLOCKED", err)


class MapEdgeTests(unittest.TestCase):
    """Selection rules that need no repository."""

    def config(self, **changes) -> select.Config:
        data = {**copy.deepcopy(CHECKS), **changes}
        return select.Config(CONTRACTS / "fixtures" / "valid" / "checks.json", data)

    def test_area_naming_a_missing_kind_is_an_error_not_an_empty_selection(self):
        config = self.config(areas=[{"glob": "a/**", "kind": "ghost"}])
        with self.assertRaises(select.ConfigError):
            select.select(config, ["a/b.cs"], "L2")

    def test_lowest_level_wins_across_kinds(self):
        config = self.config(
            areas=[{"glob": "a/**", "kind": "a"}, {"glob": "b/**", "kind": "b"}],
            kinds={"a": {"L3": ["player:ios"]}, "b": {"L1": ["player:ios"]}},
        )
        selection = select.select(config, ["a/x", "b/x"], "L4")
        self.assertEqual("L1", selection.checks[0].level)

    def test_two_runners_for_one_suite_conflict(self):
        config = self.config(
            areas=[{"glob": "a/**", "kind": "a"}, {"glob": "b/**", "kind": "b"}],
            kinds={"a": {"L2": ["editmode:X@run one {assemblies}"]}, "b": {"L2": ["editmode:X@run two {assemblies}"]}},
        )
        with self.assertRaises(select.ConfigError):
            select.select(config, ["a/x", "b/x"], "L2")

    def test_bad_entry_is_a_clean_failure(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "checks.json"
        path.write_text(json.dumps({**CHECKS, "kinds": {**CHECKS["kinds"], "docs": {"L0": ["bogus"]}}}), encoding="utf-8")
        config = select.load_config(path)
        with self.assertRaises(ValueError):
            select.select(config, ["docs/a.md"], "L0")
        self.assertEqual(1, select.run_guarded("select", lambda: select.select(config, ["docs/a.md"], "L0") and 0))

    def test_plan_without_evidence_needs_a_physical_device(self):
        plan = select.DevicePlan(Path("p.json"), "p", ("ios",), (), (), None)
        self.assertEqual(("physical",), select.required_evidence(plan))


class ShippedMapTests(unittest.TestCase):
    """The example checks map is valid apart from the plan it leaves for the project to supply."""

    def test_template_is_valid_apart_from_its_missing_plan(self):
        config = select.load_config(CORE_ROOT.parent / "examples" / "checks.json")
        errors, _ = select.validate_config(config)
        self.assertEqual(1, len(errors))
        self.assertIn("perf.open-playback", errors[0])


class ReportTests(RepoCase):
    def setUp(self):
        super().setUp()
        write(self.root, "src/media/a.cs", "changed\n")
        self.sha = self.commit()
        self.results = self.root.parent / (self.root.name + "-results")
        self.results.mkdir()
        self.addCleanup(lambda: [path.unlink() for path in self.results.glob("*.json")] and None)
        self.addCleanup(lambda: self.results.rmdir() if self.results.exists() and not any(self.results.iterdir()) else None)

    def result(self, name: str, **changes) -> Path:
        document = json.loads((FIXTURES / "result.json").read_text(encoding="utf-8"))
        document["plan"]["id"] = PLAN_KEY
        document["candidate"]["sha"] = self.sha
        for dotted, value in changes.items():
            target = document
            *parents, last = dotted.split(".")
            for part in parents:
                target = target[part]
            target[last] = value
        path = self.results / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def report(self, *extra: str, level: str = "L4") -> tuple[int, str, str]:
        return run("report", "--base-ref", "main", "--checks", str(self.checks_path), "--results", str(self.results), "--level", level, *extra)

    def test_selected_checks_without_results_are_listed_as_not_run(self):
        code, out, _ = self.report("--pr")
        self.assertEqual(2, code)
        self.assertTrue(out.startswith("## Local verification"))
        self.assertNotIn("PASS", out.replace("NOT_VERIFIED", ""))
        self.assertIn("NOT_RUN", out)
        self.assertIn(f"| `{PLAN_KEY}` | ios | NOT_RUN |", out)
        self.assertIn("not verified on ios: " + PLAN_KEY, out)
        self.assertIn("`editmode:Media.Tests`", out)

    def test_pass_with_physical_evidence_and_the_rest_not_run(self):
        self.result("pass.json")
        code, out, _ = self.report("--pr")
        self.assertEqual(2, code)
        self.assertIn(f"| `{PLAN_KEY}` | ios | PASS | physical | mac-build |", out)
        self.assertIn("1 PASS", out)
        self.assertIn("thermal state not controlled", out)

    def test_everything_passing_exits_zero(self):
        # only the device plan is selected for a result-bearing check; drop the other checks from the map
        only_plan = {**CHECKS, "kinds": {"media": {"L4": ["device:plans/open-playback"]}, "ui": {}, "docs": {}}}
        self.checks_path.write_text(json.dumps(only_plan), encoding="utf-8")
        self.result("pass.json")
        code, out, _ = self.report()
        self.assertEqual(0, code)
        self.assertNotIn("Not verified", out)

    def test_blocked_result_is_reported_with_its_reason(self):
        self.result("blocked.json", status="BLOCKED", reason="iPad is locked", **{"layers.launch": "BLOCKED"})
        code, out, _ = self.report()
        self.assertEqual(2, code)
        self.assertIn("BLOCKED", out)
        self.assertIn("not verified on ios: " + PLAN_KEY + ", BLOCKED: iPad is locked", out)

    def test_fail_dominates_the_exit_code(self):
        self.result("fail.json", status="FAIL", reason="regression", **{"layers.scenario": "FAIL"})
        self.assertEqual(1, self.report()[0])
        self.result("fail.json", status="TIMEOUT", reason="too slow", **{"layers.scenario": "TIMEOUT"})
        self.assertEqual(3, self.report()[0])

    def test_statuses_are_shown_as_they_are(self):
        for index, status in enumerate(("NEEDS_PERMISSION", "STALL", "TIMEOUT")):
            with self.subTest(status):
                self.result("one.json", status=status, reason="why", **{"layers.launch": status})
                _, out, _ = self.report()
                self.assertIn(f"| ios | {status} |", out)

    def test_result_for_another_revision_does_not_count(self):
        self.result("old.json", **{"candidate.sha": "1" * 40})
        _, out, _ = self.report()
        self.assertIn(f"| `{PLAN_KEY}` | ios | NOT_RUN |", out)
        self.assertIn("another revision: 11111111", out)

    def test_wrong_evidence_is_not_a_pass(self):
        self.result("editor.json", **{"target.evidence": "editor"})
        _, out, _ = self.report()
        self.assertIn(f"| `{PLAN_KEY}` | ios | NOT_VERIFIED | editor |", out)
        self.assertIn("not verified on ios", out)

    def test_result_for_another_platform_does_not_count(self):
        self.result("android.json", **{"target.platform": "android"})
        _, out, _ = self.report()
        self.assertIn(f"| `{PLAN_KEY}` | ios | NOT_RUN |", out)

    def test_latest_result_wins(self):
        self.result("early.json", status="FAIL", reason="flake", runId="early", finishedUtc="2026-10-29T10:00:00Z", **{"layers.scenario": "FAIL"})
        self.result("late.json", runId="late", finishedUtc="2026-10-29T11:00:00Z")
        _, out, _ = self.report("--json")
        row = next(row for row in json.loads(out)["rows"] if row["key"] == PLAN_KEY)
        self.assertEqual(("PASS", "late"), (row["status"], row["runId"]))

    def test_invalid_result_files_are_ignored_and_listed(self):
        self.result("pass.json")
        (self.results / "broken.json").write_text('{"schema": "gdv.result/1"}', encoding="utf-8")
        (self.results / "other.json").write_text('{"schema": "gdv.plan/1"}', encoding="utf-8")
        (self.results / "notes.json").write_text("not json", encoding="utf-8")
        _, out, _ = self.report("--pr")
        self.assertIn("Ignored result files:", out)
        self.assertIn("broken.json", out)
        self.assertIn("notes.json", out)
        self.assertNotIn("other.json", out)

    def test_nested_results_are_found(self):
        nested = self.results / "run1"
        nested.mkdir()
        document = json.loads((FIXTURES / "result.json").read_text(encoding="utf-8"))
        document["plan"]["id"] = PLAN_KEY
        document["candidate"]["sha"] = self.sha
        (nested / "result.json").write_text(json.dumps(document), encoding="utf-8")
        self.addCleanup(lambda: ((nested / "result.json").unlink(), nested.rmdir()))
        _, out, _ = self.report()
        self.assertIn(f"| `{PLAN_KEY}` | ios | PASS |", out)

    def test_editor_check_results_match_by_key(self):
        self.result("suite.json", **{"plan.id": "editmode:Media.Tests", "target.platform": "editor", "target.evidence": "editor"})
        _, out, _ = self.report()
        self.assertIn("| `editmode:Media.Tests` | editor | PASS | editor |", out)

    def test_missing_results_folder_is_blocked(self):
        code, _, err = run("report", "--base-ref", "main", "--checks", str(self.checks_path), "--results", str(self.results / "nope"))
        self.assertEqual(2, code)
        self.assertIn("BLOCKED", err)

    def test_json_output_lists_counts_and_not_verified(self):
        code, out, _ = self.report("--json")
        payload = json.loads(out)
        self.assertEqual(2, code)
        self.assertEqual(self.sha, payload["candidate"])
        self.assertEqual(0, payload["counts"]["PASS"])
        self.assertGreater(payload["counts"]["NOT_RUN"], 0)
        self.assertTrue(any(PLAN_KEY in line for line in payload["notVerified"]))

    def test_fail_policy_with_unclassified_paths_is_not_reported(self):
        self.checks_path.write_text(json.dumps({**CHECKS, "unclassified": "fail"}), encoding="utf-8")
        write(self.root, "other/x.txt", "changed\n")
        self.commit()
        code, _, err = self.report()
        self.assertEqual(1, code)
        self.assertIn("other/x.txt", err)

    def test_dirty_tree_result_is_not_a_pass(self):
        self.result("dirty.json", **{"candidate.dirty": True})
        _, out, _ = self.report()
        self.assertIn(f"| `{PLAN_KEY}` | ios | NOT_VERIFIED |", out)

    def test_required_platform_without_a_passing_device_row_is_not_verified(self):
        only_editor = {**CHECKS, "kinds": {"media": {"L2": [f"editmode:Media.Tests@{RUNNER}"]}, "ui": {}, "docs": {}}}
        self.checks_path.write_text(json.dumps(only_editor), encoding="utf-8")
        self.result("suite.json", **{"plan.id": "editmode:Media.Tests", "target.platform": "editor", "target.evidence": "editor"})
        code, out, _ = self.report()
        self.assertEqual(2, code)
        self.assertIn("not verified on ios: no passing device or player check", out)

    def test_unsupported_result_schema_makes_the_report_untrustworthy(self):
        only_plan = {**CHECKS, "kinds": {"media": {"L4": ["device:plans/open-playback"]}, "ui": {}, "docs": {}}}
        self.checks_path.write_text(json.dumps(only_plan), encoding="utf-8")
        self.result("pass.json")
        (self.results / "newer.json").write_text('{"schema": "gdv.result/2"}', encoding="utf-8")
        code, out, _ = self.report("--pr")
        self.assertEqual(2, code)
        self.assertIn("unsupported result schema", out)

    def test_fractional_seconds_order_by_time_not_by_text(self):
        passed = {"finishedUtc": "2026-10-29T10:00:05Z", "status": "PASS", "runId": "a"}
        failed = {"finishedUtc": "2026-10-29T10:00:05.5Z", "status": "FAIL", "runId": "b"}
        self.assertIs(failed, report._latest([passed, failed]))
        tie = {"finishedUtc": "2026-10-29T10:00:05Z", "status": "FAIL", "runId": "0"}
        self.assertIs(tie, report._latest([passed, tie]))

    def test_a_check_without_platform_shows_the_worst_platform(self):
        self.result("mac.json", **{"plan.id": "editmode:Media.Tests", "target.platform": "macos", "target.evidence": "editor",
                                   "status": "FAIL", "reason": "broken", "layers.scenario": "FAIL", "finishedUtc": "2026-10-29T10:00:00Z"})
        self.result("win.json", **{"plan.id": "editmode:Media.Tests", "target.platform": "windows", "target.evidence": "editor",
                                   "host.id": "win", "finishedUtc": "2026-10-29T11:00:00Z"})
        _, out, _ = self.report("--json")
        row = next(row for row in json.loads(out)["rows"] if row["key"] == "editmode:Media.Tests")
        self.assertEqual(("FAIL", "macos"), (row["status"], row["platform"]))

    def test_worktree_changes_downgrade_every_pass(self):
        self.result("pass.json")
        write(self.root, "docs/unsaved.md", "dirty\n")
        _, out, _ = self.report("--include-worktree", "--json")
        row = next(row for row in json.loads(out)["rows"] if row["key"] == PLAN_KEY)
        self.assertEqual("NOT_VERIFIED", row["status"])
        self.assertIn("uncommitted", row["detail"])

    def test_result_text_cannot_add_lines(self):
        self.assertEqual("a b", report._flat("a\n- forged\n"[:1] + "\n b"))
        self.assertNotIn("\n", report._cell("x\ny|z"))

    def test_impossible_date_is_an_ignored_result_and_sha_must_be_head(self):
        self.result("bad.json", finishedUtc="2026-13-45T10:00:00Z")
        code, out, _ = self.report("--pr")
        self.assertEqual(2, code)
        self.assertIn("finishedUtc is not a real date", out)
        code, _, err = self.report("--sha", "1" * 40)
        self.assertEqual(1, code)
        self.assertIn("not HEAD", err)

    def test_exit_code_helper(self):
        rows = [report.Row("a", "device", None, "PASS"), report.Row("b", "device", None, report.NOT_RUN)]
        self.assertEqual(constants.EXIT_BLOCKED, report.exit_code(rows))
        self.assertEqual(constants.EXIT_PASS, report.exit_code(rows[:1]))
        self.assertEqual(constants.EXIT_PASS, report.exit_code([]))


if __name__ == "__main__":
    unittest.main()
