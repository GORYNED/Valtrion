import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("valtrion_release_tool", ROOT / "tools" / "check_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseSourceTests(unittest.TestCase):
    def test_current_integration_package_passes_source_checks(self):
        self.assertEqual([], release.check_sources(ROOT, []))

    def test_generated_output_is_rejected_under_the_unity_integration(self):
        for path in (
                "integrations/unity/UnityProject/Library/imported.asset",
                "integrations/unity/UnityProject/Temp/test.unity",
                "integrations/unity/UnityProject/Logs/Editor.log",
                "integrations/unity/UnityProject/obj/assembly.dll",
                "integrations/unity/UnityProject/UserSettings/EditorUserSettings.asset",
                "integrations\\unity\\UnityProject\\LIBRARY\\cache.bin",
                "integrations/unity/com.goryned.valtrion.unity/Temp/compiled.dll"):
            with self.subTest(path=path):
                self.assertEqual([f"generated Unity output is tracked: {path}"], release.check_sources(ROOT, [path]))

    def test_guard_does_not_treat_other_integrations_or_core_modules_as_unity_output(self):
        self.assertEqual([], release.check_sources(ROOT, [
            "integrations/another-app/Library/source.py",
            "core/valtrion/providers/unity/support.py",
            "integrations/unity/com.goryned.valtrion.unity/Runtime/Harness/PlanRunner.cs",
        ]))
