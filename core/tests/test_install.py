import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from valtrion import __version__

ROOT = Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_file_location("valtrion_install_tool", ROOT / "tools" / "install.py")
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


class InstallToolTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._folder.cleanup)
        self.home = Path(self._folder.name)
        self.calls = []

    def run_fake(self, command, check=False):
        self.calls.append([str(part) for part in command])
        return subprocess.CompletedProcess(command, 0)

    def test_pipx_is_used_when_present_and_the_command_found_on_path(self):
        which = {"pipx": "/bin/pipx", "valtrion": "/home/x/.local/bin/valtrion"}.get
        command = install.install(False, self.run_fake, which, self.home)
        self.assertEqual(Path("/home/x/.local/bin/valtrion"), command)
        self.assertEqual(["/bin/pipx", "install", "--force"], self.calls[0][:3])
        self.assertTrue(self.calls[0][3].endswith(f"valtrion-{__version__}-py3-none-any.whl"))

    def test_without_pipx_a_venv_under_the_valtrion_home_is_used(self):
        command = install.install(False, self.run_fake, lambda name: None, self.home)
        environment, python, expected = install.venv_paths(self.home, __version__)
        self.assertEqual(expected, command)
        self.assertEqual(["-m", "venv", str(environment)], self.calls[0][1:])
        self.assertEqual([str(python), "-m", "pip", "install", "--force-reinstall", "--no-deps"], self.calls[1][:6])

    def test_venv_flag_ignores_pipx(self):
        install.install(True, self.run_fake, {"pipx": "/bin/pipx"}.get, self.home)
        self.assertNotIn("/bin/pipx", self.calls[0])

    def test_home_option_routes_the_install_to_the_requested_home(self):
        with mock.patch.object(install, "install", return_value=self.home / "valtrion") as action:
            self.assertEqual(0, install.main(["--venv", "--home", str(self.home)]))
        action.assert_called_once_with(True, home=self.home.resolve())

    def test_home_requires_venv_so_pipx_cannot_ignore_it(self):
        with self.assertRaises(SystemExit) as raised:
            install.main(["--home", str(self.home)])
        self.assertEqual(2, raised.exception.code)
