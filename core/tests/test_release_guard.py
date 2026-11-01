import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("valtrion_release_guard", ROOT / "tools" / "release_guard.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class ReleaseGuardTests(unittest.TestCase):
    def test_current_sources_pass_for_both_components(self):
        for component in ("core", "unity"):
            with self.subTest(component=component):
                version = guard.source_version(ROOT, component)
                self.assertEqual([], guard.check_release(ROOT, component, version))

    def test_wrong_or_non_final_versions_are_rejected(self):
        self.assertTrue(guard.check_release(ROOT, "core", "9.9.9"))
        self.assertTrue(any("final X.Y.Z" in p for p in guard.check_release(ROOT, "core", "0.7.4rc1")))
        self.assertTrue(guard.check_release(ROOT, "other", "1.0.0"))

    def test_tags_are_independent_per_component(self):
        self.assertEqual("core-v0.7.4", guard.tag_for("core", "0.7.4"))
        self.assertEqual("unity-v0.7.2", guard.tag_for("unity", "0.7.2"))

    def test_decide_allows_only_clean_or_resumable_states(self):
        self.assertEqual("full", guard.decide("no", "no", "none")[0])
        self.assertEqual("full", guard.decide("no", "no", "draft")[0])
        self.assertEqual("finalize", guard.decide("yes", "no", "draft")[0])

    def test_decide_refuses_every_other_state(self):
        for pypi in ("yes", "no"):
            for tag in ("yes", "no"):
                for release in ("none", "draft", "published"):
                    expected = (pypi, tag, release) in {("no", "no", "none"), ("no", "no", "draft"), ("yes", "no", "draft")}
                    with self.subTest(pypi=pypi, tag=tag, release=release):
                        self.assertEqual(expected, guard.decide(pypi, tag, release)[0] != "abort")
        self.assertEqual("abort", guard.decide("maybe", "no", "none")[0])

    def test_pypi_comparison_requires_identical_files(self):
        same = {"a.whl": "1", "a.tar.gz": "2"}
        self.assertEqual([], guard.compare_files(same, dict(same)))
        self.assertTrue(guard.compare_files(same, None))
        self.assertTrue(guard.compare_files(same, {"a.whl": "1"}))
        self.assertTrue(guard.compare_files({"a.whl": "1"}, same))
        self.assertTrue(guard.compare_files(same, {"a.whl": "1", "a.tar.gz": "x"}))

    def test_missing_pypi_version_is_reported_as_absent(self):
        import io
        import urllib.error

        def missing(url, timeout):
            raise urllib.error.HTTPError(url, 404, "Not Found", None, io.BytesIO())

        def broken(url, timeout):
            raise urllib.error.HTTPError(url, 503, "Unavailable", None, io.BytesIO())

        self.assertIsNone(guard.pypi_files("0.0.1", opener=missing))
        with self.assertRaises(urllib.error.HTTPError):
            guard.pypi_files("0.0.1", opener=broken)


if __name__ == "__main__":
    unittest.main()
