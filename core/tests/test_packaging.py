import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from valtrion import __version__, wheel


class DistributionLicenseTests(unittest.TestCase):
    def test_installed_copy_can_redistribute_the_license_when_syncing_hosts(self):
        expected = (Path(__file__).resolve().parents[2] / "LICENSE").read_bytes()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            built = wheel.build_wheel(root / "first")
            with zipfile.ZipFile(built) as archive:
                archive.extractall(root / "site")
                self.assertEqual(expected, archive.read(f"valtrion-{__version__}.dist-info/licenses/LICENSE"))
            script = (
                "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
                "from valtrion.wheel import build_wheel; build_wheel(Path(sys.argv[2]))"
            )
            subprocess.run([sys.executable, "-I", "-c", script, str(root / "site"), str(root / "second")], check=True)
            with zipfile.ZipFile(root / "second" / built.name) as archive:
                self.assertEqual(expected, archive.read("valtrion/_license.txt"))
                self.assertEqual(expected, archive.read(f"valtrion-{__version__}.dist-info/licenses/LICENSE"))
