"""Passing results of pipeline `run` stages, kept by key and reused when asked (`valtrion pipeline --reuse-results`).

A `run` stage that ends `PASS` on a clean tree is stored under the work root, `<workRoot>/result-cache/<key>.json`, where the key is
a SHA-256 over every input that decides the outcome: project, platform, commit, plan SHA-256, device class, build profile, the
Valtrion Core version and a hash of the stage itself (its arguments and the content of its pre-launch files; not its `plan` path,
whose content is the plan hash, and not its `cleanup`). Only `PASS` is stored, and a lookup trusts an entry only when:

- the file is a regular file of this user (POSIX: owned by the effective user, not writable by group or others; a link is refused),
  no larger than `MAX_ENTRY_BYTES`, inside a cache folder that is itself no link;
- it is a `valtrion.resultcache/1` entry whose key is the file name and whose recorded components are the ones asked for;
- the stored result hashes to the recorded `resultSha256`, is a valid `gdv.result/1`, is `PASS`, is not dirty, and its own
  `candidate`, `plan`, `target` and `host` fields agree with the key.

Anything else is ignored (a miss with a reason), never trusted and never repaired. `FAIL`, `BLOCKED`, `STALL`, `TIMEOUT`,
`NEEDS_PERMISSION` and stages that did not run are never stored, so they are never reused. Retention follows the build cache: entries
not used for `keep_days` and the least recently used beyond `max_entries` are removed (`gc`), and stray temporary files after a day.
"""

from __future__ import annotations

import json
import os
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from . import __version__, constants, core
from .schema import Validator, validate_document

FORMAT = "valtrion.resultcache/1"
FOLDER = "result-cache"
DEFAULT_KEEP_DAYS = 14.0
DEFAULT_MAX_ENTRIES = 500
#: A stored result larger than this is neither written nor read (a result with very many samples is simply not cached).
MAX_ENTRY_BYTES = 8 << 20
_TEMP_PREFIX = ".incoming-"
_HEX64 = frozenset("0123456789abcdef")
_REPLACE_ATTEMPTS = 20


@dataclass(frozen=True)
class Settings:
    """`host.json` `resultCache`: whether pipelines look results up without `--reuse-results` (off by default), and how long cached
    results are kept (the defaults are the built-in limits)."""
    reuse: bool = False
    keep_days: float = DEFAULT_KEEP_DAYS
    max_entries: int = DEFAULT_MAX_ENTRIES


def cache_root(work_root: Path) -> Path:
    return Path(work_root) / FOLDER


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def stage_fingerprint(stage: Mapping[str, Any], base: Path | None) -> str:
    """SHA-256 of what a run stage adds to its plan: the stage definition without `plan` (its content is the plan hash) and `cleanup`
    (removed after the result is written), plus the content hash of each pre-launch file (`missing` for one that cannot be read)."""
    definition = {key: value for key, value in stage.items() if key not in ("plan", "cleanup")}
    files = []
    for entry in stage.get("prelaunchFiles") or ():
        local = Path(str(entry.get("local", "")))
        if not local.is_absolute() and base is not None:
            local = base / local
        try:
            digest = core.sha256_file(local)
        except OSError:
            digest = "missing"
        files.append([str(entry.get("remote", "")), digest])
    return core.sha256_bytes(_canonical({"stage": definition, "prelaunch": files}))


@dataclass(frozen=True)
class Key:
    repo: str
    platform: str
    sha: str
    plan_sha256: str
    device_class: str
    profile: str
    stage_sha256: str
    core_version: str = __version__

    def components(self) -> dict[str, str]:
        return {"repo": self.repo, "platform": self.platform, "sha": self.sha, "planSha256": self.plan_sha256,
                "deviceClass": self.device_class, "profile": self.profile, "coreVersion": self.core_version,
                "stageSha256": self.stage_sha256}

    @property
    def digest(self) -> str:
        return core.sha256_bytes(_canonical(self.components()))


def cacheable_commit(sha: str | None, dirty: bool) -> bool:
    """Whether a result of this commit and tree may be stored or looked up: a real commit and a clean tree."""
    return bool(sha) and not dirty and len(sha) == 40 and set(sha) != {"0"}


@dataclass(frozen=True)
class Hit:
    key: str
    result: dict
    job_id: str
    stored_utc: str
    path: Path


def _valid_digest(digest: str) -> bool:
    return isinstance(digest, str) and len(digest) == 64 and set(digest) <= _HEX64


def _untrusted(path: Path, folder: bool = False) -> str | None:
    """Why a cache file (or the cache folder) must not be trusted, or None."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "no entry"
    except OSError as error:
        return f"unreadable ({error})"
    if stat.S_ISLNK(info.st_mode):
        return "it is a link"
    if folder and not stat.S_ISDIR(info.st_mode):
        return "the cache folder is not a folder"
    if not folder and not stat.S_ISREG(info.st_mode):
        return "it is not a regular file"
    if not folder and info.st_size > MAX_ENTRY_BYTES:
        return f"it is larger than {MAX_ENTRY_BYTES} bytes"
    if os.name != "nt":
        euid = getattr(os, "geteuid", None)
        if euid is not None and info.st_uid != euid():
            return "it belongs to another user"
        if info.st_mode & 0o022:
            return "it is writable by other users"
    return None


def _ensure_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass


def _write_private(path: Path, data: bytes) -> None:
    """Write `data` through a new temporary file of this user only (0600) in the same folder, then rename it over `path`."""
    temporary = path.with_name(f"{_TEMP_PREFIX}{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:  # Windows: a reader holds the old file for a moment
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(0.05)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def store(root: Path, key: Key, result: Mapping[str, Any], job_id: str, clock: Callable[[], str] = core.utc_now) -> Path | None:
    """Keep a passing run stage result under its key. Returns the entry, or None when the result may not be stored: not PASS, from a
    dirty tree or without a commit, not matching the key, invalid, or too large. A comparison is not stored (it is made again)."""
    document = {name: value for name, value in result.items() if name != "comparison"}
    if document.get("status") != constants.PASS or not cacheable_commit(document.get("candidate", {}).get("sha"),
                                                                           bool(document.get("candidate", {}).get("dirty", True))):
        return None
    if _mismatch(document, key) is not None:
        return None
    entry = {"format": FORMAT, "key": key.digest, "components": key.components(), "storedUtc": clock(), "jobId": job_id,
             "resultSha256": core.sha256_bytes(_canonical(document)), "result": document}
    data = core.to_json(entry).encode("utf-8")
    if len(data) > MAX_ENTRY_BYTES:
        return None
    _, errors = validate_document(document, Validator())
    if errors:
        return None
    _ensure_root(root)
    if _untrusted(root, folder=True) is not None:
        return None
    path = root / f"{key.digest}.json"
    _write_private(path, data)
    return path


def _mismatch(result: Mapping[str, Any], key: Key) -> str | None:
    """Which field of the result disagrees with the key, or None."""
    try:
        observed = {"repo": result["candidate"]["repo"], "sha": result["candidate"]["sha"], "profile": result["candidate"]["profile"],
                    "planSha256": result["plan"]["sha256"], "deviceClass": result["target"]["deviceClass"],
                    "platform": result["target"]["platform"]}
    except (KeyError, TypeError):
        return "the stored result lacks its candidate, plan or target"
    wanted = key.components()
    for name, value in observed.items():
        if value != wanted[name]:
            return f"the stored result has {name} {value!r}, not {wanted[name]!r}"
    return None


def lookup(root: Path, key: Key, validator: Validator | None = None, touch: bool = True) -> tuple[Hit | None, str]:
    """The trusted passing result for `key`, or (None, why it is a miss). See the module text for what is checked. A hit counts as
    a use for `gc` unless `touch` is False (a status query)."""
    digest = key.digest
    if not _valid_digest(digest):
        return None, "invalid key"
    problem = _untrusted(root, folder=True)
    if problem == "no entry":
        return None, "no cached result"
    if problem is not None:
        return None, f"the result cache folder is ignored: {problem}"
    path = root / f"{digest}.json"
    problem = _untrusted(path)
    if problem == "no entry":
        return None, "no cached result"
    if problem is not None:
        return None, f"the cached entry is ignored: {problem}"
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_ENTRY_BYTES + 1)
        entry = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as error:
        return None, f"the cached entry is ignored: unreadable ({type(error).__name__})"
    if len(data) > MAX_ENTRY_BYTES:
        return None, "the cached entry is ignored: it is too large"
    if not isinstance(entry, dict) or entry.get("format") != FORMAT:
        return None, "the cached entry is ignored: not a result cache entry"
    if entry.get("key") != digest or entry.get("components") != key.components():
        return None, "the cached entry is ignored: it was stored for another key"
    result = entry.get("result")
    if not isinstance(result, dict) or entry.get("resultSha256") != core.sha256_bytes(_canonical(result)):
        return None, "the cached entry is ignored: the stored result does not match its recorded hash"
    schema_id, errors = validate_document(result, validator or Validator())
    if schema_id != "gdv.result/1" or errors:
        return None, "the cached entry is ignored: the stored result is not a valid gdv.result/1"
    if result.get("status") != constants.PASS:
        return None, f"the cached entry is ignored: the stored result is {result.get('status')}, and only PASS is reused"
    if not cacheable_commit(result["candidate"]["sha"], result["candidate"]["dirty"]):
        return None, "the cached entry is ignored: the stored result is from a dirty tree or has no commit"
    problem = _mismatch(result, key)
    if problem is not None:
        return None, f"the cached entry is ignored: {problem}"
    job_id, stored = entry.get("jobId"), entry.get("storedUtc")
    if not isinstance(job_id, str) or not job_id or not isinstance(stored, str):
        return None, "the cached entry is ignored: it does not say which job stored it"
    if touch:
        try:
            os.utime(path)  # a use keeps the entry from `gc`
        except OSError:
            pass
    return Hit(digest, result, job_id, stored, path), "reused"


def gc(root: Path, keep_days: float = DEFAULT_KEEP_DAYS, max_entries: int = DEFAULT_MAX_ENTRIES, protect: tuple[str, ...] = (),
       now: Callable[[], float] = time.time, dry_run: bool = False) -> list[str]:
    """Remove entries unused (by modification time) for `keep_days`, then the least recently used above `max_entries`, and
    temporary files older than a day. Only files directly in the cache folder are touched (a link is removed, never followed).
    Returns the removed file names."""
    if keep_days < 0 or max_entries < 0:
        raise ValueError("result cache retention values cannot be negative")
    if _untrusted(root, folder=True) is not None:
        return []
    removed: list[str] = []
    entries: list[tuple[float, Path]] = []
    current = now()
    for path in root.iterdir():
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode):
            continue
        if path.name.startswith(_TEMP_PREFIX):
            if current - info.st_mtime > 86400:
                entries.append((float("-inf"), path))
            continue
        if path.suffix == ".json" and _valid_digest(path.stem):
            entries.append((info.st_mtime, path))
    entries.sort(key=lambda item: item[0])
    keep: list[Path] = []
    for used, path in entries:
        if path.stem in protect:
            keep.append(path)
            continue
        if used == float("-inf") or current - used > keep_days * 86400:
            if dry_run or _unlink(path):
                removed.append(path.name)
        else:
            keep.append(path)
    surplus = len(keep) - max_entries
    for path in keep:  # oldest first
        if surplus <= 0:
            break
        if path.stem in protect:
            continue
        if dry_run or _unlink(path):
            removed.append(path.name)
        surplus -= 1
    return removed


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except OSError:
        return False
