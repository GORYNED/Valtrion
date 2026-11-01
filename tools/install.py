#!/usr/bin/env python3
"""Install the valtrion core of this checkout on this machine (standard library only; Python >= 3.10).

    python tools/install.py            # pipx when it is installed, otherwise a virtual environment
    python tools/install.py --venv     # always the virtual environment ~/.valtrion/core/<version>

A wheel is built from the checkout (no network, no build backend) and installed with `pipx install --force`, or into the virtual
environment with `pip install --force-reinstall --no-deps`. The last line printed is the absolute path of the `valtrion` command.
Other machines are brought to the same version with `valtrion hosts sync`.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Sequence

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "core"


def venv_paths(home: Path, version: str) -> tuple[Path, Path, Path]:
    """(environment folder, its python, its valtrion command)."""
    folder = home / ".valtrion" / "core" / version
    scripts, suffix = ("Scripts", ".exe") if sys.platform.startswith("win") else ("bin", "")
    return folder, folder / scripts / ("python" + suffix), folder / scripts / ("valtrion" + suffix)


def install(use_venv: bool, run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
            which: Callable[[str], str | None] = shutil.which, home: Path | None = None, source: Path = CORE) -> Path:
    sys.path.insert(0, str(source))
    try:
        from valtrion import __version__  # type: ignore[import-not-found]
        from valtrion.wheel import build_wheel  # type: ignore[import-not-found]
    finally:
        sys.path.remove(str(source))
    with tempfile.TemporaryDirectory(prefix="valtrion-install-") as folder:
        built = build_wheel(Path(folder))
        pipx = None if use_venv else which("pipx")
        if pipx:
            run([pipx, "install", "--force", str(built)], check=True)
            command = which("valtrion")
            if command is None:
                raise SystemExit("pipx installed valtrion, but it is not on PATH; run `pipx ensurepath` and open a new terminal")
            return Path(command)
        environment, python, command = venv_paths(home or Path.home(), __version__)
        run([sys.executable, "-m", "venv", str(environment)], check=True)
        run([str(python), "-m", "pip", "install", "--force-reinstall", "--no-deps", "--disable-pip-version-check", str(built)], check=True)
        return command


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the valtrion core of this checkout on this machine.")
    parser.add_argument("--venv", action="store_true", help="use ~/.valtrion/core/<version> even when pipx is available")
    parser.add_argument("--home", type=Path, help="installation home instead of the user home (requires --venv)")
    arguments = parser.parse_args(argv)
    if arguments.home is not None and not arguments.venv:
        parser.error("--home requires --venv")
    if sys.version_info < (3, 10):
        print("valtrion needs Python 3.10 or newer", file=sys.stderr)
        return 2
    try:
        command = install(arguments.venv, home=arguments.home.resolve() if arguments.home else None)
    except subprocess.CalledProcessError as error:
        print(f"install failed: {' '.join(map(str, error.cmd))} exited with {error.returncode}", file=sys.stderr)
        return 2
    print(command)
    return 0


if __name__ == "__main__":
    sys.exit(main())
