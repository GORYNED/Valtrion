import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from valtrion import retention


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.root = Path(self._folder.name)

    def test_the_folder_is_moved_aside_with_a_manifest_that_says_what_failed_when_and_who_holds_it(self):
        data = self.root / "player-data"
        (data / "gdv").mkdir(parents=True)
        (data / "gdv" / "result.json").write_text("{}", encoding="utf-8")
        kept = retention.retain_folder(data, "STALL", "the heartbeat did not advance")
        self.assertFalse(data.exists(), "the next run starts from an empty folder")
        self.assertTrue(retention.is_retained(kept))
        self.assertTrue((kept / "gdv" / "result.json").is_file())
        manifest = json.loads((kept / retention.MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(("gdv.retained/1", "STALL", "the heartbeat did not advance"), (manifest["schema"], manifest["status"], manifest["reason"]))
        self.assertTrue(manifest["retainedUtc"].endswith("Z"))
        self.assertTrue({"user", "machine", "processId"} <= set(manifest["holder"]))

    def test_a_missing_reason_is_replaced_by_the_status_and_two_keeps_of_the_same_folder_do_not_collide(self):
        folder = self.root / "player-data"
        folder.mkdir()
        one = retention.retain_folder(folder, "FAIL", None)
        folder.mkdir()
        with mock.patch.object(retention.core, "utc_now", return_value="2026-10-30T10:00:00.000Z"):
            first = retention.retain_folder(folder, "FAIL", "x")
            folder.mkdir()
            second = retention.retain_folder(folder, "FAIL", "y")  # the same millisecond
        self.assertEqual(3, len({one, first, second}))
        self.assertEqual("the run ended FAIL", json.loads((one / retention.MANIFEST_NAME).read_text(encoding="utf-8"))["reason"])

    def test_a_manifest_that_is_already_there_keeps_saying_what_failed_first(self):
        folder = self.root / "player-data"
        folder.mkdir()
        (folder / retention.MANIFEST_NAME).write_text(json.dumps({"status": "STALL", "reason": "first"}), encoding="utf-8")
        kept = retention.retain_folder(folder, "INTERRUPTED", "second")
        self.assertEqual("first", json.loads((kept / retention.MANIFEST_NAME).read_text(encoding="utf-8"))["reason"])
        later = [p for p in kept.iterdir() if p.name.startswith("retained.") and p.name != retention.MANIFEST_NAME]
        self.assertEqual(1, len(later))
        self.assertEqual("second", json.loads(later[0].read_text(encoding="utf-8"))["reason"])

    def test_an_empty_folder_is_not_worth_keeping(self):
        folder = self.root / "player-data"
        folder.mkdir()
        self.assertFalse(retention.has_content(folder))
        (folder / "x").write_text("1", encoding="utf-8")
        self.assertTrue(retention.has_content(folder))
        self.assertFalse(retention.has_content(self.root / "missing"))

    def test_a_plain_folder_is_not_marked_retained(self):
        self.assertFalse(retention.is_retained(self.root / "player-data"))


if __name__ == "__main__":
    unittest.main()
