"""Test windows stay off the user's screen on macOS: the Editor is told to hide them, only there and only when the setting is on."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from valtrion import unityrun, windowisolation

from tests.test_host import HostTestCase, run as run_cli
from tests.test_unity_run import FAKE, UnityRunTestCase


class EnvironmentTests(unittest.TestCase):
    def test_the_variable_is_set_on_macos_when_enabled(self):
        with mock.patch.object(windowisolation, "applies", return_value=True):
            self.assertEqual("1", windowisolation.environment({"PATH": "/bin"}, True)["GDV_ISOLATE_WINDOWS"])

    def test_nothing_is_set_when_disabled(self):
        with mock.patch.object(windowisolation, "applies", return_value=True):
            self.assertNotIn("GDV_ISOLATE_WINDOWS", windowisolation.environment({"PATH": "/bin"}, False))

    def test_nothing_is_set_on_other_systems(self):
        with mock.patch.object(windowisolation, "applies", return_value=False):
            self.assertNotIn("GDV_ISOLATE_WINDOWS", windowisolation.environment({"PATH": "/bin"}, True))

    def test_a_value_inherited_from_the_caller_does_not_override_the_setting(self):
        with mock.patch.object(windowisolation, "applies", return_value=True):
            self.assertNotIn("GDV_ISOLATE_WINDOWS", windowisolation.environment({"GDV_ISOLATE_WINDOWS": "1"}, False))

    def test_the_rest_of_the_environment_is_kept(self):
        with mock.patch.object(windowisolation, "applies", return_value=True):
            self.assertEqual("/bin", windowisolation.environment({"PATH": "/bin"}, True)["PATH"])


class UnityRunTests(UnityRunTestCase):
    def _given(self, **options):
        dump = self.base / "dump"
        code, out, err = self.unity("--dump", str(dump), **options)
        self.assertEqual(0, code, err)
        return (dump / "isolation-env").read_text(encoding="utf-8")

    def test_unity_is_asked_to_hide_its_windows_on_macos(self):
        with mock.patch.object(windowisolation, "applies", return_value=True):
            self.assertEqual("1", self._given(hidden_desktop=True))

    def test_not_when_the_setting_is_off(self):
        with mock.patch.object(windowisolation, "applies", return_value=True):
            self.assertEqual("unset", self._given(hidden_desktop=False))

    def test_not_by_default_of_the_function(self):
        with mock.patch.object(windowisolation, "applies", return_value=True):
            self.assertEqual("unset", self._given())

    def test_host_json_turns_it_on(self):
        (self.home / "host.json").write_text('{"hiddenDesktop": true}', encoding="utf-8")
        with mock.patch.object(windowisolation, "applies", return_value=True):
            self.assertEqual("1", self._given())

    def test_a_value_inherited_from_the_caller_is_not_passed_on_when_the_setting_is_off(self):
        with mock.patch.object(windowisolation, "applies", return_value=True), \
                mock.patch.dict(os.environ, {"GDV_ISOLATE_WINDOWS": "1"}):
            self.assertEqual("unset", self._given(hidden_desktop=False))

    def test_a_missing_package_is_said_on_stderr(self):
        (self.project / "Packages").mkdir()
        (self.project / "Packages" / "manifest.json").write_text('{"dependencies": {}}', encoding="utf-8")
        dump = self.base / "dump"
        with mock.patch.object(windowisolation, "applies", return_value=True):
            code, out, err = self.unity("--dump", str(dump), hidden_desktop=True)
        self.assertEqual(0, code, err)
        self.assertIn("does not use com.goryned.valtrion.unity", err)


class CommandLineTests(HostTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.base / "Proj"
        (self.project / "ProjectSettings").mkdir(parents=True)

    def _isolation_given(self, *options):
        dump = self.base / "dump"
        with mock.patch.object(windowisolation, "applies", return_value=True):
            code, out, err = run_cli("unity", "run", "--unity", sys.executable, "--lease-wait", "0", *options, "--",
                                     FAKE, "-batchmode", "-projectPath", str(self.project), "--dump", str(dump))
        self.assertEqual(0, code, err)
        return (dump / "isolation-env").read_text(encoding="utf-8")

    def test_it_is_on_by_default(self):
        self.assertEqual("1", self._isolation_given())

    def test_no_hidden_desktop_turns_it_off(self):
        self.assertEqual("unset", self._isolation_given("--no-hidden-desktop"))

    def test_hidden_desktop_turns_it_on(self):
        self.assertEqual("1", self._isolation_given("--hidden-desktop"))


class NoteTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.project = Path(folder.name)
        (self.project / "Packages").mkdir()

    def _manifest(self, dependency: str):
        (self.project / "Packages" / "manifest.json").write_text(json.dumps({"dependencies": {"com.goryned.valtrion.unity": dependency}}), encoding="utf-8")

    def test_without_batchmode_it_is_said(self):
        self._manifest("https://example.com/x.git?path=p#unity-v0.13.0")
        self.assertIn("-batchmode", windowisolation.note(self.project, ["-runTests"]))

    def test_an_old_tag_is_said(self):
        self._manifest("https://example.com/x.git?path=p#unity-v0.12.0")
        self.assertIn("0.12.0", windowisolation.note(self.project, ["-batchmode"]))

    def test_a_new_enough_tag_is_quiet(self):
        self._manifest("https://example.com/x.git?path=p#unity-v0.13.0")
        self.assertIsNone(windowisolation.note(self.project, ["-batchmode"]))

    def test_an_embedded_package_is_found_and_its_version_counts(self):
        folder = self.project / "Packages" / "com.goryned.valtrion.unity"
        folder.mkdir()
        (folder / "package.json").write_text('{"version": "0.12.0"}', encoding="utf-8")
        self.assertIn("0.12.0", windowisolation.note(self.project, ["-batchmode"]))
        (folder / "package.json").write_text('{"version": "0.13.1"}', encoding="utf-8")
        self.assertIsNone(windowisolation.note(self.project, ["-batchmode"]))

    def test_another_dependencys_tag_is_not_taken_for_the_package(self):
        (self.project / "Packages" / "manifest.json").write_text(json.dumps({"dependencies": {
            "com.goryned.valtrion.unity": "file:../package", "com.example.other": "https://example.com/x.git#unity-v0.1.0"}}), encoding="utf-8")
        self.assertIsNone(windowisolation.note(self.project, ["-batchmode"]))

    def test_a_manifest_that_is_not_json_is_not_reported(self):
        (self.project / "Packages" / "manifest.json").write_text("{", encoding="utf-8")
        self.assertIsNone(windowisolation.note(self.project, ["-batchmode"]))

    def test_a_local_path_is_assumed_to_be_new_enough(self):
        self._manifest("file:../package")
        self.assertIsNone(windowisolation.note(self.project, ["-batchmode"]))

    def test_an_unreadable_project_is_not_reported(self):
        (self.project / "Packages" / "manifest.json").unlink(missing_ok=True)
        self.assertIsNone(windowisolation.note(self.project, ["-batchmode"]))


if __name__ == "__main__":
    unittest.main()