import contextlib
import io
import unittest
from pathlib import Path

from valtrion import adapter, cli, constants

CORE_ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = CORE_ROOT.parent / "contracts"
RESERVED = ("install", "launch", "push", "pull", "screenshot", "logs")


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def test_every_reserved_command_exits_with_two(self):
        for name in RESERVED:
            with self.subTest(name):
                code, _, err = run(name)
                self.assertEqual(2, code)
                self.assertIn("not implemented", err)

    def test_reserved_commands_accept_arbitrary_arguments(self):
        for arguments in (("install", "--plan", "x"), ("launch", "acquire", "-h")):
            with self.subTest(arguments):
                code, _, err = run(*arguments)
                self.assertEqual(2, code)
                self.assertIn("not implemented", err)

    def test_validate_accepts_a_valid_fixture(self):
        code, out, _ = run("validate", str(CONTRACTS / "fixtures" / "valid" / "plan.json"))
        self.assertEqual(0, code)
        self.assertIn("gdv.plan/1", out)

    def test_validate_rejects_an_invalid_fixture(self):
        code, _, err = run("validate", str(CONTRACTS / "fixtures" / "invalid" / "plan-bad-kind.json"))
        self.assertEqual(1, code)
        self.assertIn("kind", err)

    def test_validate_applies_the_plan_rule_for_external_ui(self):
        code, _, err = run("validate", str(CONTRACTS / "fixtures" / "invalid" / "plan-external-ui-in-probe.json"))
        self.assertEqual(1, code)
        self.assertIn("external.ui", err)

    def test_validate_reports_unreadable_input_as_blocked(self):
        import tempfile
        code, _, err = run("validate", str(CONTRACTS / "fixtures" / "valid" / "missing.json"))
        self.assertEqual(2, code)
        self.assertIn("BLOCKED", err)
        with tempfile.TemporaryDirectory() as folder:
            undecodable = Path(folder) / "binary.json"
            undecodable.write_bytes(b"\xff\xfe\x00\x80")
            code, _, err = run("validate", str(undecodable))
            self.assertEqual(2, code)
            self.assertIn("BLOCKED", err)
            code, _, err = run("validate", folder)  # exists but is not a readable file
            self.assertEqual(2, code)
            malformed = Path(folder) / "malformed.json"
            malformed.write_text("{", encoding="utf-8")
            self.assertEqual(1, run("validate", str(malformed))[0])  # unreadable is BLOCKED, malformed is a FAIL

    def test_schemas_lists_all_contracts(self):
        code, out, _ = run("schemas")
        self.assertEqual(0, code)
        self.assertEqual(list(constants.SCHEMA_IDS), out.split())

    def test_exit_code_command(self):
        self.assertEqual((0, "3\n"), run("exit-code", "TIMEOUT")[:2])

    def test_no_command_prints_help_and_is_blocked(self):
        code, _, err = run()
        self.assertEqual(2, code)
        self.assertIn("usage", err)

    def test_a_later_registration_replaces_a_stub(self):
        previous = cli.COMMANDS["build"]
        self.addCleanup(cli.register, previous)
        cli.register(cli.Command("build", "real", "platform adapters", lambda _: 0))
        self.assertEqual(0, run("build")[0])


class AdapterTests(unittest.TestCase):
    def test_platform_adapter_is_abstract(self):
        with self.assertRaises(TypeError):
            adapter.PlatformAdapter()

    def test_incomplete_adapter_cannot_be_created(self):
        class Partial(adapter.PlatformAdapter):
            platform = "ios"

            def probe(self):
                return adapter.Capabilities("ios")

        with self.assertRaises(TypeError):
            Partial()

    def test_outcome_requires_a_reason_unless_pass(self):
        adapter.StepOutcome(constants.PASS)
        with self.assertRaises(ValueError):
            adapter.StepOutcome(constants.BLOCKED)
        self.assertFalse(adapter.StepOutcome(constants.BLOCKED, "device locked").ok)

    def test_outcome_rejects_unknown_status(self):
        with self.assertRaises(ValueError):
            adapter.StepOutcome("OK")

    def test_device_info_rejects_unknown_evidence(self):
        with self.assertRaises(ValueError):
            adapter.DeviceInfo("d", "ios", "ipad", "simulated")

    def test_os_major(self):
        self.assertEqual(27, adapter.DeviceInfo("d", "ios", "ipad", "physical", os_version="iPadOS 27.2").os_major)
        self.assertEqual(14, adapter.DeviceInfo("d", "android", "p", "physical", os_version="14").os_major)
        self.assertEqual(0, adapter.DeviceInfo("d", "android", "p", "physical").os_major)
        self.assertEqual(17, adapter.DeviceInfo("d", "ios", "p", "physical", os_version="17.2 (21C62)").os_major)
        self.assertEqual(3, adapter.DeviceInfo("d", "ios", "p", "physical", os_version="3²").os_major)
        self.assertEqual(0, adapter.DeviceInfo("d", "ios", "p", "physical", os_version="9" * 40).os_major)

    def test_capabilities_respect_blocked(self):
        capabilities = adapter.Capabilities("ios", ("build", "install"), {"install": "device locked"})
        self.assertTrue(capabilities.can("build"))
        self.assertFalse(capabilities.can("install"))
        self.assertFalse(capabilities.can("launch"))


class ForegroundDomainTests(unittest.TestCase):
    def test_foreground_with_an_explicit_domain_is_a_usage_error_before_anything_starts(self):
        sha = "a" * 40
        for argv in (("run", "plan.json", "--repo", "r", "--sha", sha, "--app", "a", "--foreground", "--domain", "gui"),
                     ("run", "plan.json", "--repo", "r", "--sha", sha, "--app", "a", "--domain", "user", "--foreground"),
                     ("pipeline", "p.json", "--foreground", "--domain", "gui"),
                     ("build", "--repo", "r", "--platform", "ios", "--foreground", "--domain", "gui"),
                     ("job", "start", "--timeout-sec", "5", "--foreground", "--domain", "gui", "--", "true")):
            with self.subTest(argv):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        cli.main(list(argv))
                self.assertEqual(2, raised.exception.code)
                self.assertIn("--foreground runs the job in this process", err.getvalue())
                self.assertIn("is not for signing", err.getvalue())

    def test_foreground_with_the_default_domain_is_not_refused_by_the_parser(self):
        arguments = cli.build_parser().parse_args(["job", "start", "--timeout-sec", "5", "--foreground", "--", "true"])
        self.assertTrue(arguments.foreground)
        self.assertEqual("auto", arguments.domain)


if __name__ == "__main__":
    unittest.main()
