"""The copies of installed apps that Apple's device service keeps on a Mac: pruning, host.json, gc, disk pressure and doctor."""

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from valtrion import cli, config, core, devicecache, diskspace, doctor
from tests.test_diskspace import FakeVolume


class CacheCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.cache = self.base / "AppInstallationBinaryDeltas"
        self.home = self.base / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {core.HOME_ENV: str(self.home), devicecache.CACHE_ENV: str(self.cache)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def settings_file(self, disk: dict | None = None) -> None:
        document = {"artifactsRoot": str(self.base / "artifacts"), "workRoot": str(self.base / "work")}
        if disk is not None:
            document["disk"] = disk
        (self.home / "host.json").write_text(json.dumps(document), encoding="utf-8")

    def copy(self, app: str, name: str, age_days: float, size: int = 10) -> Path:
        folder = self.cache / app / name
        folder.mkdir(parents=True)
        (folder / "app.bin").write_bytes(b"x" * size)
        stamp = time.time() - age_days * 86400
        for item in (folder / "app.bin", folder):
            os.utime(item, (stamp, stamp))
        return folder


class PruneTests(CacheCase):
    def test_the_newest_copies_of_each_app_stay(self):
        old, mid, new = self.copy("com.example.One", "a", 5), self.copy("com.example.One", "b", 3), self.copy("com.example.One", "c", 1)
        other = self.copy("com.example.Two", "d", 9)
        report = devicecache.prune(self.cache, 2)
        self.assertEqual([str(old)], report.removed)
        self.assertEqual(10, report.freed)
        self.assertEqual(3, report.kept)
        self.assertFalse(old.exists())
        self.assertTrue(mid.exists() and new.exists() and other.exists())
        self.assertEqual([], report.errors)

    def test_a_dry_run_removes_nothing(self):
        old = self.copy("com.example.One", "a", 5)
        self.copy("com.example.One", "b", 1)
        report = devicecache.prune(self.cache, 1, dry_run=True)
        self.assertEqual([str(old)], report.removed)
        self.assertTrue(old.exists())

    def test_null_leaves_the_cache_alone_and_zero_removes_every_copy(self):
        first, second = self.copy("com.example.One", "a", 5), self.copy("com.example.One", "b", 1)
        self.assertEqual([], devicecache.prune(self.cache, None).removed)
        self.assertTrue(first.exists() and second.exists())
        self.assertEqual(2, len(devicecache.prune(self.cache, 0).removed))
        self.assertFalse(first.exists() or second.exists())
        self.assertTrue((self.cache / "com.example.One").is_dir())  # the app's folder stays

    def test_a_missing_folder_or_no_folder_is_not_an_error(self):
        self.assertEqual([], devicecache.prune(self.cache, 1).errors)
        self.assertEqual([], devicecache.prune(None, 1).errors)
        self.assertEqual(0, devicecache.size_bytes(self.cache))
        self.assertFalse(devicecache.present(None))

    def test_a_link_is_never_followed(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("data", encoding="utf-8")
        newest = self.copy("com.example.One", "z", 0)
        link = self.cache / "com.example.One" / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks are not available")
        stamp = time.time() - 30 * 86400
        os.utime(link, (stamp, stamp), follow_symlinks=False)
        report = devicecache.prune(self.cache, 1)
        self.assertTrue((outside / "keep.txt").exists())
        self.assertTrue(newest.exists())
        self.assertFalse(link.exists() or link.is_symlink())
        self.assertEqual([], report.errors)

    def test_a_folder_that_cannot_be_read_is_reported_not_raised(self):
        self.copy("com.example.One", "a", 5)
        with mock.patch.object(Path, "iterdir", side_effect=PermissionError("Operation not permitted")):
            report = devicecache.prune(self.cache, 1)
        self.assertEqual([], report.removed)
        self.assertIn("Full Disk Access", report.errors[0])

    def test_the_default_root_follows_the_environment(self):
        self.assertEqual(self.cache, devicecache.default_root())
        with mock.patch.dict(os.environ, {devicecache.CACHE_ENV: ""}):
            self.assertIsNone(devicecache.default_root())


class HostSettingsTests(CacheCase):
    def test_the_default_keeps_two_copies_and_null_turns_it_off(self):
        self.settings_file()
        self.assertEqual(2, config.load_host_settings(self.home).device_install_cache_keep)
        self.settings_file({"deviceInstallCacheKeep": 5})
        self.assertEqual(5, config.load_host_settings(self.home).device_install_cache_keep)
        self.settings_file({"deviceInstallCacheKeep": None})
        self.assertIsNone(config.load_host_settings(self.home).device_install_cache_keep)

    def test_a_bad_value_is_a_host_json_error(self):
        for bad in (-1, 1.5, "2", True):
            self.settings_file({"deviceInstallCacheKeep": bad})
            with self.assertRaises(config.ConfigError, msg=repr(bad)):
                config.load_host_settings(self.home)


class CleanupTests(CacheCase):
    def test_gc_removes_old_copies_and_says_so(self):
        self.settings_file({"deviceInstallCacheKeep": 1})
        old = self.copy("com.example.One", "a", 5, size=1000)
        new = self.copy("com.example.One", "b", 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(0, cli.main(["gc", "--dry-run"]))
        self.assertTrue(old.exists())
        self.assertIn("would remove", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(0, cli.main(["gc"]))
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertIn("1 device service copie(s)", out.getvalue())

    def test_gc_with_null_leaves_the_copies(self):
        self.settings_file({"deviceInstallCacheKeep": None})
        old = self.copy("com.example.One", "a", 5)
        self.copy("com.example.One", "b", 1)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(0, cli.main(["gc"]))
        self.assertTrue(old.exists())

    def test_pressure_removes_old_copies_before_it_blocks(self):
        self.settings_file({"minFreeGiB": 10, "deviceInstallCacheKeep": 1})
        settings = config.load_host_settings(self.home)
        old = self.copy("com.example.One", "a", 5)
        self.copy("com.example.One", "b", 1)
        volume = FakeVolume(5)
        volume.hold(old, 6)
        outcome = diskspace.ensure_free(settings, self.home, free=volume)
        self.assertTrue(outcome.ok, outcome.reason)
        self.assertIn(f"{old} (device service copy)", outcome.removed)

    def test_the_blocked_reason_names_the_copies_as_a_consumer(self):
        self.settings_file({"minFreeGiB": 10, "deviceInstallCacheKeep": None})
        settings = config.load_host_settings(self.home)
        self.copy("com.example.One", "a", 5, size=5000)
        names = [name for name, _ in diskspace.largest_consumers(settings, self.home, limit=10)]
        self.assertTrue(any("device service copies of installed apps" in name for name in names), names)

    def test_doctor_reports_the_copies(self):
        self.settings_file()
        self.copy("com.example.One", "a", 5, size=2048)
        self.copy("com.example.One", "b", 1, size=2048)
        report = doctor.storage_report(self.base / "work", self.base / "artifacts", None, self.home)
        copies = report["deviceServiceCopies"]
        self.assertEqual((str(self.cache), 2), (copies["path"], copies["copies"]))
        self.assertEqual(4096, copies["bytes"])
        self.assertNotIn("error", copies)
        text = doctor.render({
            "host": {"id": "mac", "os": "macos"},
            "capabilities": {"unityEditors": [], "buildPlatforms": [], "iosSigning": False, "adb": False},
            "disk": {"freeGb": 1, "totalGb": 2, "path": "/"}, "githubRunner": {"state": "none"}, "blocked": {},
            "storage": {**report, "workRoot": {"bytes": 0}, "artifactsRoot": {"bytes": 0}, "buildCache": {"bytes": 0, "entries": 0}}})
        self.assertIn("device service copies of installed apps", text)

    def test_doctor_has_no_entry_without_the_folder(self):
        self.settings_file()
        report = doctor.storage_report(self.base / "work", self.base / "artifacts", None, self.home)
        self.assertNotIn("deviceServiceCopies", report)


if __name__ == "__main__":
    unittest.main()
