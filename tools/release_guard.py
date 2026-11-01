"""Guards used by the manual Release workflow (.github/workflows/release.yml).

Standard library only; Python 3.10 or newer. Nothing here publishes anything.

    python tools/release_guard.py check --component core --version 0.7.4
    python tools/release_guard.py decide --pypi yes --tag no --release draft
    python tools/release_guard.py pypi-state --version 0.7.4
    python tools/release_guard.py verify-pypi --version 0.7.4 --dir dist
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNITY_PACKAGE = ROOT / "integrations" / "unity" / "com.goryned.valtrion.unity"
FINAL_VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
TAG_PREFIX = {"core": "core-v", "unity": "unity-v"}


def tag_for(component: str, version: str) -> str:
    return TAG_PREFIX[component] + version


def source_version(root: Path, component: str) -> str:
    if component == "core":
        text = (root / "core" / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'(?m)^version\s*=\s*"([^"\n]+)"\s*$', text)
        if match is None:
            raise ValueError("core/pyproject.toml has no quoted version")
        return match.group(1)
    package = root / "integrations" / "unity" / "com.goryned.valtrion.unity" / "package.json"
    return json.loads(package.read_text(encoding="utf-8"))["version"]


def check_release(root: Path, component: str, version: str) -> list[str]:
    """Return problems that must stop a release of this component and version."""
    problems: list[str] = []
    if component not in TAG_PREFIX:
        return [f"unknown component {component!r}"]
    if not FINAL_VERSION.fullmatch(version):
        problems.append(f"version {version!r} must be a final X.Y.Z version (pre-release status is the GitHub Release flag)")
    actual = source_version(root, component)
    if actual != version:
        problems.append(f"requested {component} version {version} differs from the source version {actual}")
    if component == "core":
        metadata = (root / "core" / "pyproject.toml").read_text(encoding="utf-8")
        if "Private :: Do Not Upload" in metadata:
            problems.append("core/pyproject.toml still has the 'Private :: Do Not Upload' classifier, which PyPI rejects")
        init = (root / "core" / "valtrion" / "__init__.py").read_text(encoding="utf-8")
        if f'__version__ = "{version}"' not in init:
            problems.append("core/valtrion/__init__.py __version__ differs from the requested version")
        changelog = root / "CHANGELOG.md"
    else:
        changelog = root / "integrations" / "unity" / "com.goryned.valtrion.unity" / "CHANGELOG.md"
    if not changelog.is_file() or not re.search(rf"(?m)^## {re.escape(version)}(\s|$)", changelog.read_text(encoding="utf-8")):
        problems.append(f"{changelog.relative_to(root).as_posix()} has no '## {version}' section")
    return problems


def decide(pypi: str, tag: str, release: str) -> tuple[str, str]:
    """Pick the Core release mode from the existing external state.

    pypi: yes|no (this version is on PyPI); tag: yes|no; release: none|draft|published.
    Returns (mode, reason). Modes: full, finalize, abort.
    """
    if pypi not in ("yes", "no") or tag not in ("yes", "no") or release not in ("none", "draft", "published"):
        return "abort", "unrecognized state"
    if pypi == "no":
        if tag == "no" and release in ("none", "draft"):
            return "full", "nothing is published; a stale draft Release, if any, is replaced"
        return "abort", "a tag or published Release exists without the PyPI version: resolve by hand, never move a tag"
    if tag == "no" and release == "draft":
        return "finalize", "PyPI has this version and a draft Release holds the uploaded files: finish the Release only"
    return "abort", "this version is already published; bump the version for a new release"


def pypi_files(version: str, opener=urllib.request.urlopen) -> dict[str, str] | None:
    """Files PyPI holds for this version as {filename: sha256}, or None when the version does not exist."""
    url = f"https://pypi.org/pypi/valtrion/{version}/json"
    try:
        with opener(url, timeout=30) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    return {item["filename"]: item["digests"]["sha256"] for item in data["urls"]}


def compare_files(local: dict[str, str], remote: dict[str, str] | None) -> list[str]:
    """Problems when the local distribution files are not exactly what PyPI holds."""
    if remote is None:
        return ["PyPI does not have this version"]
    problems = [f"{name}: PyPI has no such file" for name in sorted(set(local) - set(remote))]
    problems += [f"{name}: on PyPI but not in the Release files" for name in sorted(set(remote) - set(local))]
    problems += [f"{name}: sha256 differs from PyPI" for name in sorted(set(local) & set(remote)) if local[name] != remote[name]]
    return problems


def hash_directory(folder: Path) -> dict[str, str]:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(folder.iterdir()) if path.suffix == ".whl" or path.name.endswith(".tar.gz")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--component", required=True, choices=sorted(TAG_PREFIX))
    check.add_argument("--version", required=True)
    pick = sub.add_parser("decide")
    pick.add_argument("--pypi", required=True)
    pick.add_argument("--tag", required=True)
    pick.add_argument("--release", required=True)
    state = sub.add_parser("pypi-state")
    state.add_argument("--version", required=True)
    verify = sub.add_parser("verify-pypi")
    verify.add_argument("--version", required=True)
    verify.add_argument("--dir", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "pypi-state":
        print("yes" if pypi_files(args.version) is not None else "no")
        return 0
    if args.command == "verify-pypi":
        local = hash_directory(args.dir)
        problems = compare_files(local, pypi_files(args.version)) if local else ["no distribution files found"]
        for problem in problems:
            print(f"FAIL {problem}")
        return 1 if problems else 0
    if args.command == "check":
        problems = check_release(ROOT, args.component, args.version)
        for problem in problems:
            print(f"FAIL {problem}")
        print(f"tag={tag_for(args.component, args.version)}" if not problems else "release guard: stop")
        return 1 if problems else 0
    mode, reason = decide(args.pypi, args.tag, args.release)
    print(f"mode={mode}")
    print(reason, file=sys.stderr)
    return 1 if mode == "abort" else 0


if __name__ == "__main__":
    sys.exit(main())
