"""Published builds: the artifact of a `build` stage kept outside the job's work folder, and reused by key.

A build is published into `<home>/builds/<key>/` (`manifest.json` plus the artifact under `artifact/`). The work root that
holds the build output is removed with the job (`jobs.cleanup_work`); the published copy is not. The key of a clean tree is a hash
of project, commit, platform, profile and toolchain (Unity version, adapter toolchain such as the Xcode version, a hash of the
profile configuration), so the same clean commit with the same profile and tools is built once. A dirty tree is published too
(its artifact can be installed by key and is reported in the result) but under a key of its own that no later build looks up.

Retention is by last use: `gc` removes entries unused for `keep_days` (dirty ones for `dirty_keep_days`) and the least recently
used beyond `max_entries`, never an entry used within the last `GUARD_SEC` or one it is told to protect.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import core

MANIFEST = "manifest.json"
ARTIFACT_FOLDER = "artifact"
SCHEMA = "valtrion.build/1"
DEFAULT_KEEP_DAYS = 14.0
DEFAULT_DIRTY_KEEP_DAYS = 2.0
DEFAULT_MAX_ENTRIES = 20
#: An entry used (published, reused or installed from) within this many seconds is never removed: a job may be installing it.
GUARD_SEC = 3600.0
_INCOMING = ".incoming-"


def builds_root(home: Path | None = None) -> Path:
    return (home or core.valtrion_home()) / "builds"


def profile_config_hash(project_dir: Path | None, profile: str, profiles_file: Path = Path("validation") / "device" / "profiles.json") -> str:
    """Hash of what the project says about the build profile: its entry in `validation/device/profiles.json` (all platforms), or
    the whole file when it cannot be read as JSON, or `none` when the project has no such file. A changed profile is a new key."""
    if project_dir is None:
        return "none"
    try:
        data = (project_dir / profiles_file).read_bytes()
    except OSError:
        return "none"
    try:
        document = json.loads(data.decode("utf-8-sig"))
        entry = document.get("profiles", {}).get(profile) if isinstance(document, dict) else None
        if entry is not None:
            return core.sha256_bytes(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    except ValueError:
        pass
    return core.sha256_bytes(data)


def compute_key(project: str, platform: str, sha: str, profile: str, toolchain: Mapping[str, str], config_hash: str,
                device_id: str | None = None) -> str:
    """The cache key: 32 hex characters over every input that changes the produced build. `device_id` is part of the key for a
    platform whose build is signed for one device."""
    material = {"project": project, "platform": platform, "sha": sha, "profile": profile, "toolchain": dict(sorted(toolchain.items())),
                "config": config_hash, "device": device_id or ""}
    return core.sha256_bytes(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8"))[:32]


def dirty_key(job_id: str) -> str:
    return "dirty-" + core.safe_segment(job_id, "job", 80)


def tree_digest(path: Path) -> tuple[str, int, str]:
    """(sha256, size in bytes, kind) of a file, or of a folder (a hash over its sorted relative paths and file hashes)."""
    if path.is_file():
        return core.sha256_file(path), path.stat().st_size, "file"
    digest = hashlib.sha256()
    size = 0
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        relative = item.relative_to(path).as_posix()
        digest.update(f"{relative}\0{core.sha256_file(item)}\n".encode("utf-8"))
        size += item.stat().st_size
    return digest.hexdigest(), size, "directory"


@dataclass(frozen=True)
class Entry:
    key: str
    directory: Path
    manifest: dict

    @property
    def path(self) -> Path:
        return self.item / self.manifest["entry"] if self.manifest.get("entry") else self.item

    @property
    def item(self) -> Path:
        """The moved build output (the artifact itself, or the folder that holds it)."""
        return self.directory / ARTIFACT_FOLDER / self.manifest["name"]

    @property
    def cacheable(self) -> bool:
        return bool(self.manifest.get("cacheable"))

    def describe(self) -> dict:
        """What `result.json` (`build`) and the `build.published` event say about this build."""
        manifest = self.manifest
        description = {
            "key": self.key, "path": str(self.path), "kind": manifest["kind"], "sha256": manifest["sha256"], "sizeBytes": manifest["sizeBytes"],
            "candidateSha": manifest["sha"], "dirty": manifest["dirty"], "profile": manifest["profile"], "platform": manifest["platform"]}
        if manifest.get("toolchain"):
            description["toolchain"] = dict(manifest["toolchain"])
        return description


def _read_entry(directory: Path) -> Entry | None:
    try:
        manifest = core.read_json(directory / MANIFEST, attempts=3)
        if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
            return None
        entry = Entry(directory.name, directory, manifest)
        return entry if entry.path.exists() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def find(root: Path, key: str) -> Entry | None:
    """The published build with this key (clean or dirty), or None when it is unknown or its artifact is gone."""
    if not key or key != core.safe_segment(key, "x", 200) or key.startswith(_INCOMING):
        return None
    return _read_entry(root / key)


def lookup(root: Path, key: str) -> Entry | None:
    """A reusable build for a cache key: published from a clean tree and still intact (the total size of the files is
    compared with the one recorded; a truncated or extended build is not trusted, a same-size edit is not detected)."""
    entry = find(root, key)
    if entry is None or not entry.cacheable or not _intact(entry):
        return None
    return entry


def _intact(entry: Entry) -> bool:
    try:
        _, size, _ = _quick_shape(entry.item)
    except OSError:
        return False
    return size == entry.manifest.get("itemSizeBytes", entry.manifest["sizeBytes"])


def _quick_shape(path: Path) -> tuple[str, int, str]:
    if path.is_file():
        return "", path.stat().st_size, "file"
    return "", sum(p.stat().st_size for p in path.rglob("*") if p.is_file()), "directory"


def touch(entry: Entry, clock: Callable[[], str] = core.utc_now) -> None:
    """Record a use (a reuse or an install by key), which keeps the entry from `gc`."""
    try:
        manifest = {**entry.manifest, "lastUsedUtc": clock()}
        core.atomic_write_json(entry.directory / MANIFEST, manifest)
    except OSError:
        pass  # a failed bookkeeping write must not fail the run that uses the build


def publish(root: Path, key: str, artifact: Path, *, out: Path, project: str, platform: str, sha: str, profile: str, dirty: bool,
            toolchain: Mapping[str, str], config_hash: str, job_id: str, cacheable: bool, replace: bool = False) -> Entry:
    """Move the build output that holds `artifact` to `<root>/<key>/artifact/` and write its manifest. `artifact` must lie inside
    `out`, the job's build output folder: a path outside it (the Editor platform's artifact is the project itself) is never moved.
    A file artifact takes its whole folder along (a Windows player needs its `_Data` folder beside the exe); a folder artifact
    (`.app`, `.xcarchive`) moves alone. If another job published the same key meanwhile, that build is returned. When publishing
    fails the output is put back where it was."""
    artifact, out = artifact.resolve(), out.resolve()
    try:
        artifact.relative_to(out)
    except ValueError:
        raise ValueError(f"the artifact {artifact} is not inside the build output {out}, so it is not moved") from None
    if not artifact.exists():
        raise ValueError(f"the artifact {artifact} does not exist")
    item = artifact.parent if artifact.is_file() else artifact
    entry_path = artifact.relative_to(item).as_posix() if artifact != item else ""
    root.mkdir(parents=True, exist_ok=True)
    final = root / key
    if final.exists():
        existing = _read_entry(final)
        if existing is not None and cacheable and not replace and _intact(existing):
            return existing
        shutil.rmtree(final, ignore_errors=True)  # a broken, rejected, forced or one-off entry in the way (publishing is an atomic rename)
    incoming = Path(tempfile.mkdtemp(prefix=_INCOMING, dir=root))
    moved = incoming / ARTIFACT_FOLDER / item.name
    original = item
    done = False
    try:
        (incoming / ARTIFACT_FOLDER).mkdir()
        digest, size, kind = tree_digest(artifact)
        _, item_size, _ = _quick_shape(item)
        shutil.move(str(item), str(moved))
        stamp = core.utc_now()
        core.atomic_write_json(incoming / MANIFEST, {
            "schema": SCHEMA, "key": key, "name": item.name, "entry": entry_path, "kind": kind, "sha256": digest, "sizeBytes": size,
            "itemSizeBytes": item_size, "project": project, "platform": platform, "sha": sha, "profile": profile, "dirty": dirty,
            "cacheable": cacheable, "toolchain": dict(toolchain), "configHash": config_hash, "builtByJob": job_id, "publishedUtc": stamp,
            "lastUsedUtc": stamp})
        try:
            os.rename(incoming, final)
        except OSError:
            raced = _read_entry(final)
            if raced is None:
                raise
            shutil.rmtree(incoming, ignore_errors=True)
            done = True
            return raced
        done = True
    finally:
        if not done:
            if moved.exists() and not original.exists():
                try:
                    shutil.move(str(moved), str(original))  # give the build output back to the job
                except OSError:
                    pass
            if not moved.exists():  # a copy that could not be given back stays (swept by `gc` after a day) rather than being lost
                shutil.rmtree(incoming, ignore_errors=True)
    published = _read_entry(final)
    if published is None:
        raise OSError(f"the published build {final} cannot be read back")
    return published


def entries(root: Path) -> list[Entry]:
    if not root.is_dir():
        return []
    found = []
    for directory in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(_INCOMING)):
        entry = _read_entry(directory)
        if entry is not None:
            found.append(entry)
    return found


@dataclass(frozen=True)
class GcReport:
    removed: tuple[str, ...]
    kept: tuple[tuple[str, str], ...]


def _last_used(entry: Entry) -> datetime:
    for field_name in ("lastUsedUtc", "publishedUtc"):
        try:
            return core.parse_utc(entry.manifest[field_name])
        except (KeyError, ValueError, TypeError):
            continue
    return datetime.fromtimestamp(0, tz=core.now().tzinfo)


def gc(root: Path, keep_days: float = DEFAULT_KEEP_DAYS, dirty_keep_days: float = DEFAULT_DIRTY_KEEP_DAYS,
       max_entries: int = DEFAULT_MAX_ENTRIES, dry_run: bool = False, protect: Iterable[str] = (),
       clock: Callable[[], datetime] = core.now) -> GcReport:
    """Remove builds unused for `keep_days` (dirty ones for `dirty_keep_days`), then the least recently used above `max_entries`.
    Abandoned `.incoming-*` folders older than a day (a publisher that died) go too."""
    if keep_days < 0 or dirty_keep_days < 0 or max_entries < 0:
        raise ValueError("retention values cannot be negative")
    now = clock()
    protected = set(protect)
    removed: list[str] = []
    kept: list[tuple[str, str]] = []

    def remove(entry: Entry, why: str) -> None:
        if dry_run:
            removed.append(f"{entry.key} ({why})")
            return
        try:
            shutil.rmtree(entry.directory)
        except OSError as error:
            kept.append((entry.key, f"could not be removed: {error}"))
            return
        removed.append(f"{entry.key} ({why})")

    survivors: list[Entry] = []
    for entry in sorted(entries(root), key=_last_used):
        age = now - _last_used(entry)
        if entry.key in protected or age < timedelta(seconds=GUARD_SEC):
            kept.append((entry.key, "in use or protected"))
            survivors.append(entry)
            continue
        limit = keep_days if entry.cacheable else dirty_keep_days
        if age > timedelta(days=limit):
            remove(entry, f"unused for {age.days} days")
        else:
            survivors.append(entry)
    surplus = len(survivors) - max_entries
    for entry in survivors:  # oldest use first
        if surplus <= 0:
            break
        if entry.key in protected or now - _last_used(entry) < timedelta(seconds=GUARD_SEC):
            continue
        remove(entry, f"more than {max_entries} builds kept")
        surplus -= 1
    if not dry_run and root.is_dir():
        cutoff = time.time() - 86400
        readable = {entry.directory for entry in entries(root)}
        broken = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(_INCOMING) and p not in readable]
        for folder in [*root.glob(_INCOMING + "*"), *broken]:  # also entries without a readable manifest or artifact
            try:
                if folder.is_dir() and folder.stat().st_mtime < cutoff:
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                pass
    return GcReport(tuple(removed), tuple(kept))


def describe_all(root: Path) -> list[dict[str, Any]]:
    return [{**entry.describe(), "lastUsedUtc": entry.manifest.get("lastUsedUtc"), "cacheable": entry.cacheable} for entry in entries(root)]
