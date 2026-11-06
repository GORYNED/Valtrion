"""Test windows stay off the user's screen on macOS: the Editor is told to hide them, only there and only when the setting is on."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from valtrion import unityrun, windowisolation

from tests.test_unity_run import UnityRunTestCase


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

    def test_this_system_is_known(self):
        self.assertEqual(os.uname().sysname == "Darwin" if hasattr(os, "uname") else False, windowisolation.applies())


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


if __name__ == "__main__":
    unittest.main()
