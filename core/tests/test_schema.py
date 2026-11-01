import json
import tempfile
import unittest
from pathlib import Path

from valtrion import constants
from valtrion.schema import SchemaError, UnreadableInput, Validator, check_plan_semantics, validate_document, validate_file

CORE_ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = CORE_ROOT.parent / "contracts"
FIXTURES = CONTRACTS / "fixtures"


def fixture_files(folder: str) -> list[Path]:
    return sorted(path for path in (FIXTURES / folder).iterdir() if path.suffix in (".json", ".jsonl"))


class GoldenFixtureTests(unittest.TestCase):
    def test_every_valid_fixture_is_accepted(self):
        files = fixture_files("valid")
        self.assertGreaterEqual(len(files), len(constants.SCHEMA_IDS))
        for path in files:
            with self.subTest(path.name):
                schema_id, errors = validate_file(path)
                self.assertEqual([], errors)
                self.assertIn(schema_id, constants.SCHEMA_IDS)

    def test_every_schema_has_a_valid_fixture(self):
        seen = set()
        for path in fixture_files("valid"):
            schema_id, _ = validate_file(path)
            seen.add(schema_id)
        self.assertEqual(set(constants.SCHEMA_IDS), seen)

    def test_every_invalid_fixture_is_rejected(self):
        files = fixture_files("invalid")
        self.assertGreaterEqual(len(files), len(constants.SCHEMA_IDS))
        for path in files:
            with self.subTest(path.name):
                _, errors = validate_file(path)
                self.assertNotEqual([], errors)

    def test_every_schema_has_an_invalid_fixture(self):
        prefixes = {path.name.split("-", 1)[0] for path in fixture_files("invalid")}
        for schema_id in constants.SCHEMA_IDS:
            self.assertIn(schema_id.split(".")[1].split("/")[0], prefixes)

    def test_the_c_sharp_reader_enforces_the_same_rules_as_this_validator(self):
        # Semantic rules live in both implementations; this guards the fixtures that only they reject.
        for name in ("plan-external-ui-in-probe", "result-pass-with-reason", "result-pass-with-failed-layer",
                     "result-blocked-without-reason", "hosts-duplicate-id", "hosts-ssh-without-target", "hosts-ssh-target-option"):
            with self.subTest(name):
                self.assertTrue((FIXTURES / "invalid" / f"{name}.json").is_file())
                self.assertNotEqual([], validate_file(FIXTURES / "invalid" / f"{name}.json")[1])

    def test_strict_parsing_matches_the_runtime_reader(self):
        valid = (FIXTURES / "valid" / "event.json").read_text(encoding="utf-8")
        cases = {
            "trailing newline in an identifier": valid.replace('"job-20261029T1530Z-1"', '"job-1\\n"'),
            "trailing newline in a timestamp": valid.replace('"2026-10-29T15:31:24Z"', '"2026-10-29T15:31:24Z\\n"'),
            "duplicate property": valid.replace('"seq": 7,', '"seq": 7, "seq": 8,'),
            "NaN": valid.replace('"seq": 7', '"seq": NaN'),
            "number beyond the exact integer range": valid.replace('"seq": 7', '"seq": 1e300'),
            "number out of range": valid.replace('"seq": 7', '"seq": 1e999'),
        }
        with tempfile.TemporaryDirectory() as folder:
            for name, text in cases.items():
                with self.subTest(name):
                    path = Path(folder) / "case.json"
                    path.write_text(text, encoding="utf-8")
                    self.assertNotEqual([], validate_file(path)[1])

    def test_limits_match_the_runtime_reader(self):
        plan = (FIXTURES / "valid" / "plan.json").read_text(encoding="utf-8")
        hosts = (FIXTURES / "valid" / "hosts.json").read_text(encoding="utf-8")
        deep = '{"schema": "gdv.event/1", "seq": 0, "utc": "2026-10-29T15:31:24Z", "jobId": "j", "type": "log", "data": ' \
            + '{"a": ' * 70 + "1" + "}" * 70 + "}"
        cases = {
            "budget beyond int32": plan.replace('"budgetSeconds": 900', '"budgetSeconds": 4294967296'),
            "step timeout beyond int32": plan.replace('"timeoutSec": 180', '"timeoutSec": 4294967297'),
            "ssh target with a newline": hosts.replace('"builder@mac-build.example"', '"a\\nb"'),
            "ssh target starting with a dash": hosts.replace('"builder@mac-build.example"', '"-oProxyCommand=x"'),
            "nested deeper than 64": deep,
            "400-digit number": plan.replace('"budgetSeconds": 900', '"budgetSeconds": 1' + "0" * 400),
        }
        with tempfile.TemporaryDirectory() as folder:
            for name, text in cases.items():
                with self.subTest(name):
                    path = Path(folder) / "case.json"
                    path.write_text(text, encoding="utf-8")
                    self.assertNotEqual([], validate_file(path)[1])

    def test_unreadable_and_extremely_nested_files_are_violations_not_crashes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.json"
            path.write_bytes(b"\xff\xfe\x00")
            with self.assertRaises(UnreadableInput):
                validate_file(path)
            with self.assertRaises(UnreadableInput):
                validate_file(Path(folder) / "missing.json")
            with self.assertRaises(UnreadableInput):
                validate_file(Path(folder))  # a directory cannot be read as a file
            path.write_text("[" * 200000, encoding="utf-8")
            self.assertNotEqual([], validate_file(path)[1])

    def test_events_log_lines_may_contain_unicode_line_separators(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            event = json.loads((FIXTURES / "valid" / "event.json").read_text(encoding="utf-8"))
            event["message"] = "a\u2028b\u0085c\u2029d"
            path.write_text(json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")
            self.assertEqual([], validate_file(path)[1])

    def test_only_json_whitespace_makes_a_blank_events_line(self):
        event = (FIXTURES / "valid" / "event.json").read_text(encoding="utf-8")
        compact = json.dumps(json.loads(event))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            path.write_text(compact + "\n \t\r\n" + compact + "\n", encoding="utf-8")
            self.assertEqual([], validate_file(path)[1])
            path.write_text(compact + "\n\\u0085\n".replace("\\u0085", chr(0x85)) + compact + "\n", encoding="utf-8")
            self.assertNotEqual([], validate_file(path)[1])

    def test_a_utf8_byte_order_mark_is_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "event.json"
            path.write_bytes(b"\xef\xbb\xbf" + (FIXTURES / "valid" / "event.json").read_bytes())
            self.assertEqual([], validate_file(path)[1])


class ValidatorTests(unittest.TestCase):
    def test_schema_files_use_only_supported_keywords(self):
        # Validating a document only visits the branches it reaches, so walk every schema tree itself.
        files = sorted((CONTRACTS / "schemas").glob("*.schema.json"))
        self.assertGreaterEqual(len(files), len(constants.SCHEMA_IDS) + 1)
        for path in files:
            self._assert_supported(json.loads(path.read_text(encoding="utf-8")), path.name)

    def _assert_supported(self, node, name):
        from valtrion.schema import _SUPPORTED
        for key, value in node.items():
            if key in ("properties", "$defs"):
                for child in value.values():
                    self._assert_supported(child, name)
                continue
            self.assertIn(key, _SUPPORTED, f"{name}: unsupported keyword {key}")
            if key in ("items", "additionalProperties") and isinstance(value, dict):
                self._assert_supported(value, name)
            elif key == "anyOf":
                for child in value:
                    self._assert_supported(child, name)

    def test_unsupported_keyword_is_an_error_not_a_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / "plan.schema.json").write_text(json.dumps({"type": "object", "oneOf": []}), encoding="utf-8")
            with self.assertRaises(SchemaError):
                Validator(directory).validate({}, "gdv.plan/1")

    def test_a_corrupt_schema_file_is_a_schema_error_not_a_bad_document(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            for content in (b"{ broken", b"\xff\xfe\x00"):
                (directory / "plan.schema.json").write_bytes(content)
                with self.assertRaises(SchemaError):
                    validate_file(FIXTURES / "valid" / "plan.json", Validator(directory))

    def test_unknown_schema_id_is_rejected(self):
        schema_id, errors = validate_document({"schema": "gdv.nothing/1"})
        self.assertEqual("gdv.nothing/1", schema_id)
        self.assertTrue(errors)

    def test_missing_schema_property_is_rejected(self):
        _, errors = validate_document({"id": "x"})
        self.assertTrue(errors)

    def test_booleans_are_not_integers(self):
        plan = json.loads((FIXTURES / "valid" / "plan.json").read_text(encoding="utf-8"))
        plan["budgetSeconds"] = True
        _, errors = validate_document(plan)
        self.assertTrue(errors)

    def test_null_reason_and_string_reason_are_both_valid(self):
        result = json.loads((FIXTURES / "valid" / "result.json").read_text(encoding="utf-8"))
        result["status"] = "BLOCKED"
        result["reason"] = "device locked"
        self.assertEqual([], validate_document(result)[1])

    def test_free_form_step_arguments_are_preserved_by_the_schema(self):
        plan = json.loads((FIXTURES / "valid" / "plan.json").read_text(encoding="utf-8"))
        plan["steps"][0]["anything"] = {"deep": [1, {"x": None}]}
        self.assertEqual([], validate_document(plan)[1])

    def test_jsonl_reports_the_failing_line(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            good = (FIXTURES / "valid" / "event.json").read_text(encoding="utf-8")
            path.write_text(json.dumps(json.loads(good)) + "\n" + '{"schema":"gdv.event/1"}\n', encoding="utf-8")
            _, errors = validate_file(path)
            self.assertTrue(errors)
            self.assertTrue(all(message.startswith("line 2") for message in errors))

    def test_invalid_json_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "broken.json"
            path.write_text("{", encoding="utf-8")
            self.assertTrue(validate_file(path)[1])

    def test_external_ui_is_allowed_outside_the_probe_window(self):
        plan = {"kind": "performance", "steps": [
            {"type": "external.ui"}, {"type": "probe.start"}, {"type": "probe.stop"}, {"type": "external.ui"}]}
        self.assertEqual([], check_plan_semantics(plan))

    def test_external_ui_is_allowed_in_a_functional_plan(self):
        plan = {"kind": "functional", "steps": [{"type": "probe.start"}, {"type": "external.ui"}]}
        self.assertEqual([], check_plan_semantics(plan))


class ConstantsTests(unittest.TestCase):
    def test_exit_codes(self):
        self.assertEqual(0, constants.exit_code_for_status("PASS"))
        self.assertEqual(1, constants.exit_code_for_status("FAIL"))
        self.assertEqual(2, constants.exit_code_for_status("BLOCKED"))
        self.assertEqual(2, constants.exit_code_for_status("NEEDS_PERMISSION"))
        self.assertEqual(3, constants.exit_code_for_status("STALL"))
        self.assertEqual(3, constants.exit_code_for_status("TIMEOUT"))

    def test_every_status_has_an_exit_code(self):
        for status in constants.STATUSES:
            self.assertIn(constants.exit_code_for_status(status), (0, 1, 2, 3))

    def test_unknown_status_raises(self):
        with self.assertRaises(ValueError):
            constants.exit_code_for_status("OK")

    def test_verdict_exit_codes(self):
        self.assertEqual(1, constants.exit_code_for_verdict("REGRESSION"))
        self.assertEqual(2, constants.exit_code_for_verdict("NOT_COMPARABLE"))
        self.assertEqual(0, constants.exit_code_for_verdict("IMPROVED"))
        for verdict in constants.VERDICTS:
            constants.exit_code_for_verdict(verdict)


if __name__ == "__main__":
    unittest.main()
