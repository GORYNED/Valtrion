"""Builds the installable wheel of this very package with the standard library, from the files that are running.

`valtrion hosts sync` and `tools/install.py` need a wheel of the version on this machine, from a checkout or from an installed copy,
without a build backend or network access. The wheel holds the package modules, the contract schemas as `valtrion/_schemas` and the
compatibility table as `valtrion/_compat.json`, exactly like the one `hatchling` builds from `core/pyproject.toml`.
"""

from __future__ import annotations

import base64
import hashlib
import zipfile
from pathlib import Path

from . import __version__
from .project import compat_file
from .schema import default_schemas_directory

NAME = "valtrion"
_TIMESTAMP = (2020, 1, 1, 0, 0, 0)  # fixed, so the same sources give the same wheel
_METADATA = (
    "Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    "Summary: Repeatable checks, supervised execution and verifiable evidence on local and SSH hosts.\n"
    "License: MIT\nRequires-Python: >=3.10\n")
_WHEEL = "Wheel-Version: 1.0\nGenerator: valtrion.wheel\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
_ENTRY_POINTS = "[console_scripts]\nvaltrion = valtrion.cli:main\n"


def wheel_name(version: str = __version__) -> str:
    return f"{NAME}-{version}-py3-none-any.whl"


def package_files() -> list[tuple[str, bytes]]:
    """(path inside the wheel, content) of everything the package consists of, sorted."""
    root = Path(__file__).resolve().parent
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        files[f"{NAME}/{path.relative_to(root).as_posix()}"] = path.read_bytes()
    schemas = default_schemas_directory()
    for path in sorted(schemas.glob("*.json")):
        files[f"{NAME}/_schemas/{path.name}"] = path.read_bytes()
    compat = compat_file()
    if compat is not None:
        files[f"{NAME}/_compat.json"] = compat.read_bytes()
    license_file = next(path for path in (root / "_license.txt", root.parent / "LICENSE", root.parent.parent / "LICENSE")
                        if path.is_file())
    files[f"{NAME}/_license.txt"] = license_file.read_bytes()
    return sorted(files.items())


def _record_line(path: str, content: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode("ascii")
    return f"{path},sha256={digest},{len(content)}"


def build_wheel(destination: Path, version: str = __version__) -> Path:
    """Write `<destination>/valtrion-<version>-py3-none-any.whl` and return its path."""
    destination.mkdir(parents=True, exist_ok=True)
    dist_info = f"{NAME}-{version}.dist-info"
    entries = package_files()
    entries += [
        (f"{dist_info}/licenses/LICENSE", dict(entries)[f"{NAME}/_license.txt"]),
        (f"{dist_info}/METADATA", _METADATA.format(name=NAME, version=version).encode("utf-8")),
        (f"{dist_info}/WHEEL", _WHEEL.encode("utf-8")),
        (f"{dist_info}/entry_points.txt", _ENTRY_POINTS.encode("utf-8")),
    ]
    record = [_record_line(path, content) for path, content in entries] + [f"{dist_info}/RECORD,,"]
    entries.append((f"{dist_info}/RECORD", ("\n".join(record) + "\n").encode("utf-8")))
    target = destination / wheel_name(version)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in entries:
            info = zipfile.ZipInfo(path, _TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)
    return target
