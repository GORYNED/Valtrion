"""The export manifest: files Unity rewrote with the same bytes keep their old mtime (fake files only)."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from valtrion import incremental

OLD = 1_700_000_000_000_000_000  # ns
NEW = 1_800_000_000_000_000_000


class SettleTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="gdv-incr-test-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.export = self.folder / "export"
        self.export.mkdir()

    def put(self, rel, text, mtime=OLD):
        path = self.export / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        os.utime(path, ns=(mtime, mtime))
        return path

    def mtime(self, rel):
        return (self.export / rel).stat().st_mtime_ns

    def first_export(self):
        self.put("il2cppOutput/a.cpp", "aaa")
        self.put("il2cppOutput/b.cpp", "bbb")
        self.put("Info.plist", "plist")
        return incremental.settle(self.folder, self.export, incremental.snapshot(self.folder, self.export))

    def test_a_file_rewritten_with_the_same_bytes_gets_its_old_mtime_back(self):
        self.first_export()
        before = incremental.snapshot(self.folder, self.export)
        self.put("il2cppOutput/a.cpp", "aaa", NEW)
        stats = incremental.settle(self.folder, self.export, before)
        self.assertEqual(OLD, self.mtime("il2cppOutput/a.cpp"))
        self.assertEqual(1, stats["total"]["restored"])
        self.assertEqual(2, stats["total"]["untouched"])
        self.assertEqual(1, stats["byFolder"]["il2cppOutput"]["restored"])
        self.assertEqual(1, stats["byExtension"][".cpp"]["restored"])

    def test_changed_and_new_files_keep_their_new_mtime(self):
        self.first_export()
        before = incremental.snapshot(self.folder, self.export)
        self.put("il2cppOutput/a.cpp", "AAA", NEW)  # same size, other bytes
        self.put("il2cppOutput/b.cpp", "longer", NEW)  # other size
        self.put("il2cppOutput/c.cpp", "ccc", NEW)
        stats = incremental.settle(self.folder, self.export, before)
        for rel in ("il2cppOutput/a.cpp", "il2cppOutput/b.cpp", "il2cppOutput/c.cpp"):
            self.assertEqual(NEW, self.mtime(rel), rel)
        self.assertEqual({"changed": 2, "new": 1, "untouched": 1}, {k: v for k, v in stats["total"].items() if not k.endswith("Bytes")})

    def test_a_deleted_file_leaves_the_manifest_and_is_counted(self):
        self.first_export()
        before = incremental.snapshot(self.folder, self.export)
        (self.export / "il2cppOutput" / "b.cpp").unlink()
        stats = incremental.settle(self.folder, self.export, before)
        self.assertEqual(1, stats["total"]["deleted"])
        self.assertNotIn("il2cppOutput/b.cpp", incremental.load_manifest(self.folder))
        self.assertIn("il2cppOutput/a.cpp", incremental.load_manifest(self.folder))

    def test_the_manifest_matches_the_export_and_is_reused_without_hashing(self):
        self.first_export()
        manifest = incremental.load_manifest(self.folder)
        self.assertEqual({"il2cppOutput/a.cpp", "il2cppOutput/b.cpp", "Info.plist"}, set(manifest))
        self.assertEqual([3, OLD], manifest["il2cppOutput/a.cpp"][:2])
        # A forged hash for a file whose size and mtime still match is taken as is: the manifest is the cache.
        manifest["Info.plist"][2] = "forged"
        incremental.core.atomic_write_json(self.folder / incremental.MANIFEST, {"files": manifest})
        self.assertEqual("forged", incremental.snapshot(self.folder, self.export)["Info.plist"][2])
        # A file that was touched since is hashed again.
        self.put("Info.plist", "plist", NEW)
        self.assertNotEqual("forged", incremental.snapshot(self.folder, self.export)["Info.plist"][2])

    def test_without_a_manifest_the_existing_export_is_hashed_as_the_baseline(self):
        self.put("a.cpp", "aaa")
        before = incremental.snapshot(self.folder, self.export)
        self.put("a.cpp", "aaa", NEW)
        incremental.settle(self.folder, self.export, before)
        self.assertEqual(OLD, self.mtime("a.cpp"))

    def test_a_clean_build_resets_the_manifest(self):
        self.first_export()
        self.assertTrue((self.folder / incremental.MANIFEST).exists())
        incremental.discard(self.folder)
        self.assertFalse((self.folder / incremental.MANIFEST).exists())
        self.assertEqual({}, incremental.load_manifest(self.folder))

    def test_an_unreadable_manifest_counts_as_none(self):
        (self.folder / incremental.MANIFEST).write_text("{broken", encoding="utf-8")
        self.assertEqual({}, incremental.load_manifest(self.folder))

    def test_symlinks_are_ignored(self):
        self.put("a.cpp", "aaa")
        try:
            (self.export / "link.cpp").symlink_to(self.export / "a.cpp")
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")
        self.assertEqual({"a.cpp"}, set(incremental.snapshot(self.folder, self.export)))


if __name__ == "__main__":
    unittest.main()
