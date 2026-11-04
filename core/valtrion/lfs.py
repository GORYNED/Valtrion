"""Git LFS preflight of `valtrion unity run`: LFS pointer files left in the project's working tree are turned into their content before
Unity starts, from a cache that every project, worktree and checkout of the host shares.

A checkout made without the LFS smudge filter (`GIT_LFS_SKIP_SMUDGE=1`, a `git worktree add` with the filter switched off, a failed
download) leaves the small pointer text (`version https://git-lfs.github.com/spec/v1`, `oid sha256:<hex>`, `size <n>`) where an asset
belongs, and Unity then imports text instead of a texture, a model or a scene. The preflight:

1. Asks git whether any `filter=lfs` attribute applies (the tracked `.gitattributes` files and `info/attributes`). Without one nothing
   else runs: a project without LFS costs a few `git` calls and needs no git-lfs.
2. Lists the tracked files of the Unity project folder that have `filter=lfs` (`git check-attr`) and reads those of at most 1024 bytes;
   a file whose text is a valid pointer is one to materialize. git-lfs itself is not needed for this.
3. Takes each object, in this order, from the shared cache `<work root>/lfs-cache/objects/<aa>/<bb>/<oid>`, from the repository's own
   LFS store (`<git common dir>/lfs/objects/...`, which a linked worktree shares with its main checkout) and only then from the remote:
   one `git lfs fetch --include <the missing paths>` per batch of paths, never for an object that is already on the host. With every
   object cached nothing touches the network.
4. Copies the object into a temporary file while hashing it, checks its SHA-256 and size against the pointer, and renames it over the
   pointer (a partial file is never left in the tree; a cache object that fails the check is removed from the cache and the next
   source is tried). Objects are linked (or copied) into the cache and into the repository's store, so the next checkout reuses them
   and git-lfs's clean filter has nothing to copy.
5. Refreshes the index (`git update-index -q --refresh`) and checks that `git status` shows none of the materialized paths. A path that
   shows up is put back to its pointer and reported.

Failures carry stable codes (`CODE_*`), the failed paths and a reason; `unity run` reports them as `BLOCKED` before Unity starts.
`host.json` `lfs.materialize: false` (or `unity run --no-lfs-materialize`) turns the preflight off.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import checkout, compare, diskspace

POINTER_MAX_BYTES = 1024
POINTER_VERSION = b"version https://git-lfs.github.com/spec/v1"
CACHE_FOLDER = "lfs-cache"
#: Free space kept on a volume beyond what the objects need.
DISK_MARGIN_BYTES = 64 * 1024 * 1024
GIT_TIMEOUT_SEC = 600.0
FETCH_TIMEOUT_SEC = 3600.0
#: Longest `--include` value of one `git lfs fetch` (a Windows command line holds about 32 000 characters).
INCLUDE_CHUNK_CHARS = 8000
#: Temporary files of a run that died are removed by the next one after this long.
STALE_TEMP_SEC = 3600.0
TEMP_FOLDER = "valtrion-lfs-tmp"
_COPY_CHUNK = 1024 * 1024

OK = "ok"
SKIPPED = "skipped"
CODE_GIT_LFS_MISSING = "lfs.git-lfs-missing"
CODE_AUTH = "lfs.auth-failed"
CODE_NETWORK = "lfs.network-failed"
CODE_OBJECT_MISSING = "lfs.object-missing"
CODE_FETCH_FAILED = "lfs.fetch-failed"
CODE_HASH_MISMATCH = "lfs.hash-mismatch"
CODE_DISK_SPACE = "lfs.disk-space"
CODE_WRITE_FAILED = "lfs.write-failed"
CODE_STATUS_DIRTY = "lfs.status-dirty"
CODE_UNSUPPORTED = "lfs.unsupported-pointer"
CODE_GIT_FAILED = "lfs.git-failed"
#: When several files fail for different reasons, the outcome carries the first of these.
PRECEDENCE = (CODE_GIT_FAILED, CODE_GIT_LFS_MISSING, CODE_DISK_SPACE, CODE_AUTH, CODE_NETWORK, CODE_OBJECT_MISSING, CODE_FETCH_FAILED,
              CODE_HASH_MISMATCH, CODE_WRITE_FAILED, CODE_STATUS_DIRTY, CODE_UNSUPPORTED)

_OID = re.compile(rb"oid sha256:([0-9a-f]{64})")
_SIZE = re.compile(rb"size (0|[1-9][0-9]{0,18})")
_PATTERN_SPECIALS = re.compile(r"[*?\[\]\\,!#{}]")

#: `(cwd, argv, stdin)` -> (exit code, stdout bytes, stderr text) of `git <argv>`. Replaced in tests.
GitCall = Callable[[Path, Sequence[str], "bytes | None"], "tuple[int, bytes, str]"]
#: `(cwd, argv)` -> (exit code, output) of `git lfs <argv>`: the only calls that may reach the network. Replaced in tests.
LfsCall = Callable[[Path, Sequence[str]], "tuple[int, str]"]


def _environment() -> dict[str, str]:
    environment = compare.git_environment()  # no inherited GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE, no prompt
    if os.name != "nt":
        environment["PATH"] = environment.get("PATH", "") + os.pathsep + "/opt/homebrew/bin:/usr/local/bin"  # git-lfs of Homebrew
    return environment


def run_git(cwd: Path, argv: Sequence[str], stdin: bytes | None = None) -> tuple[int, bytes, str]:
    try:
        done = subprocess.run(["git", *argv], cwd=cwd, input=stdin if stdin is not None else b"", capture_output=True,
                              timeout=GIT_TIMEOUT_SEC, check=False, env=_environment())
    except subprocess.TimeoutExpired:
        return 124, b"", f"git {argv[0] if argv else ''} timed out after {GIT_TIMEOUT_SEC:g} s"
    except (OSError, subprocess.SubprocessError) as error:
        return 127, b"", str(error)
    return done.returncode, done.stdout, done.stderr.decode("utf-8", "replace").strip()


def run_lfs(cwd: Path, argv: Sequence[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(["git", "lfs", *argv], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=FETCH_TIMEOUT_SEC, check=False, env=_environment(), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, f"git lfs {argv[0] if argv else ''} timed out after {FETCH_TIMEOUT_SEC:g} s"
    except (OSError, subprocess.SubprocessError) as error:
        return 127, str(error)
    return done.returncode, (done.stdout + done.stderr).strip()


def cache_root(work_root: Path) -> Path:
    """The shared object cache of the host: `<work root>/lfs-cache`."""
    return Path(work_root) / CACHE_FOLDER


def object_path(root: Path, oid: str) -> Path:
    """Where an object lives below `root` (the cache, or a repository's `lfs/objects`): `<aa>/<bb>/<oid>`."""
    return root / oid[0:2] / oid[2:4] / oid


@dataclass(frozen=True)
class Pointer:
    path: str  # repository-relative, `/`-separated
    oid: str
    size: int
    raw: bytes  # the pointer text as it is in the working tree (restored when a placement must be undone)
    extensions: bool = False
    mode: str = "100644"  # the index entry of the path: mode and blob (the committed pointer)
    blob: str = ""


def parse_pointer(data: bytes) -> tuple[str, int, bool] | None:
    """(oid, size, has extensions) of a Git LFS pointer text, or None when `data` is not one."""
    if len(data) > POINTER_MAX_BYTES or not data.startswith(POINTER_VERSION + b"\n"):
        return None
    lines = data.replace(b"\r\n", b"\n").rstrip(b"\n").split(b"\n")
    oid = size = None
    extensions = False
    for line in lines[1:]:
        if line.startswith(b"ext-"):
            extensions = True
        elif (match := _OID.fullmatch(line)) is not None:
            oid = match.group(1).decode("ascii")
        elif (match := _SIZE.fullmatch(line)) is not None:
            size = int(match.group(1))
        else:
            return None
    if oid is None or size is None:
        return None
    return oid, size, extensions


@dataclass
class Outcome:
    code: str  # OK, SKIPPED or one of the CODE_* values
    reason: str | None = None
    pointers: int = 0
    materialized: list[str] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)  # materialized path -> "cache", "repository" or "fetched"
    fetch_calls: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)  # (path, code)
    notes: list[str] = field(default_factory=list)

    def _count(self, kind: str) -> int:
        return sum(1 for value in self.sources.values() if value == kind)

    @property
    def from_cache(self) -> int:
        return self._count("cache")

    @property
    def from_repository(self) -> int:
        return self._count("repository")

    @property
    def fetched(self) -> int:
        return self._count("fetched")

    @property
    def ok(self) -> bool:
        return self.code in (OK, SKIPPED)

    def describe(self) -> dict:
        """The `LFS-PREFLIGHT` record of `unity run`."""
        record: dict = {"code": self.code, "pointers": self.pointers, "materialized": len(self.materialized),
                        "fromCache": self.from_cache, "fromRepository": self.from_repository, "fetched": self.fetched,
                        "fetchCalls": self.fetch_calls}
        if self.failed:
            record["failed"] = [{"path": path, "code": code} for path, code in self.failed]
        if self.reason:
            record["reason"] = self.reason
        return record


def _text_runner(git: GitCall) -> compare.GitRunner:
    def run(folder: Path, arguments: Sequence[str]) -> tuple[int, str]:
        code, out, err = git(folder, arguments, None)
        return code, (out.decode("utf-8", "replace").rstrip() if code == 0 else err)
    return run


def _fail(outcome: Outcome, pointers: Iterable[Pointer], code: str, reason: str) -> Outcome:
    outcome.failed.extend((item.path, code) for item in pointers)
    outcome.notes.append(reason)
    return outcome


def _finish(outcome: Outcome) -> Outcome:
    if not outcome.failed:
        outcome.code = OK
        return outcome
    codes = {code for _, code in outcome.failed}
    outcome.code = next(code for code in PRECEDENCE if code in codes)
    shown = ", ".join(path for path, _ in outcome.failed[:10])
    more = f" and {len(outcome.failed) - 10} more" if len(outcome.failed) > 10 else ""
    outcome.notes = [note for note in dict.fromkeys(outcome.notes) if note]
    outcome.reason = (f"Git LFS preflight failed [{outcome.code}]: {len(outcome.failed)} of {outcome.pointers} LFS pointer file(s) could "
                      f"not be materialized ({shown}{more}): " + "; ".join(outcome.notes))
    return outcome


def classify_fetch_error(text: str) -> str:
    """The stable code of a failed `git lfs fetch`, from its output."""
    lowered = text.lower()
    if any(marker in lowered for marker in ("does not exist on the server", "object does not exist", "object not found",
                                            "missing object", "[404]")):
        return CODE_OBJECT_MISSING
    if any(marker in lowered for marker in ("authentication", "authorization", "credentials", "unauthorized", "forbidden",
                                            "permission denied", "access denied", "401", "403")):
        return CODE_AUTH
    if any(marker in lowered for marker in ("could not resolve", "no such host", "connection refused", "connection reset", "timed out",
                                            "timeout", "network is unreachable", "dial tcp", "unable to access", "failed to connect",
                                            "tls handshake", "proxyconnect", "eof")):
        return CODE_NETWORK
    return CODE_FETCH_FAILED


def include_pattern(path: str) -> str:
    """A `git lfs fetch --include` pattern that matches `path`: characters the pattern list or wildmatch would read specially (`,`, `*`,
    `[`, a backslash, ...) are replaced by `?`, which matches any one character."""
    return _PATTERN_SPECIALS.sub("?", path)


def _chunks(patterns: Sequence[str], limit: int = INCLUDE_CHUNK_CHARS) -> list[list[str]]:
    batches: list[list[str]] = [[]]
    length = 0
    for pattern in patterns:
        if batches[-1] and length + len(pattern) + 1 > limit:
            batches.append([])
            length = 0
        batches[-1].append(pattern)
        length += len(pattern) + 1
    return [batch for batch in batches if batch]


def _volume(path: Path) -> tuple[int, Path]:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return os.stat(probe).st_dev, probe
    except OSError:
        return -1, probe


class _Copy:
    """A verified copy of an object: hashed while it is written to a temporary file, renamed into place only when it matches."""

    @staticmethod
    def to_temp(source: Path, oid: str, size: int, temp: Path) -> str | None:
        """Copy `source` to `temp`; None when its SHA-256 and size are `oid` and `size`, else why not (`temp` is then removed)."""
        digest = hashlib.sha256()
        written = 0
        try:
            temp.parent.mkdir(parents=True, exist_ok=True)
            with open(source, "rb") as reader, open(temp, "wb") as writer:
                while True:
                    block = reader.read(_COPY_CHUNK)
                    if not block:
                        break
                    digest.update(block)
                    written += len(block)
                    writer.write(block)
                writer.flush()
                os.fsync(writer.fileno())
        except OSError as error:
            _unlink(temp)
            raise error
        if written != size or digest.hexdigest() != oid:
            _unlink(temp)
            return f"{source} has {written} bytes with SHA-256 {digest.hexdigest()[:12]}, not {size} bytes with {oid[:12]}"
        return None


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _temp_name(folder: Path, name: str) -> Path:
    return folder / f".{name}.valtrion-lfs.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"


def _sweep_temp(folder: Path) -> None:
    """Remove temporary files a run that died left in the temp folder of the git dir."""
    try:
        entries = list(folder.iterdir())
    except OSError:
        return
    cutoff = time.time() - STALE_TEMP_SEC
    for entry in entries:
        try:
            if entry.is_file() and entry.name.endswith(".tmp") and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue


def _link_or_copy(source: Path, target: Path, oid: str, size: int) -> bool:
    """Put a verified object at `target` (the cache or a repository store): a hard link when the volume allows, else a verified copy;
    always through a temporary name and a rename. False when it could not be placed (the caller goes on: it is an optimisation)."""
    if target.is_file():
        return True
    temp = _temp_name(target.parent, target.name)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, temp)
        except OSError:
            if _Copy.to_temp(source, oid, size, temp) is not None:
                return False
        os.replace(temp, target)
        return True
    except OSError:
        _unlink(temp)
        return False


def _scan(project: Path, top: Path, git: GitCall, outcome: Outcome) -> list[Pointer] | None:
    """The unmaterialized pointers among the tracked `filter=lfs` files of the project folder; None when git could not list them."""
    code, listed, error = git(project, ["ls-files", "-z", "-s", "--full-name", "--", "."], None)
    if code != 0:
        outcome.notes.append(f"git ls-files failed: {error[:200]}")
        return None
    paths: list[str] = []
    entries: dict[str, tuple[str, str]] = {}
    for entry in listed.split(b"\0"):
        if not entry:
            continue
        meta, _, name = entry.partition(b"\t")
        parts = meta.split()
        if len(parts) == 3 and parts[0] in (b"100644", b"100755") and parts[2] == b"0":
            paths.append(name.decode("utf-8", "surrogateescape"))
            entries[paths[-1]] = (parts[0].decode("ascii"), parts[1].decode("ascii"))
    if not paths:
        return []
    feed = b"\0".join(path.encode("utf-8", "surrogateescape") for path in paths) + b"\0"
    code, attributes, error = git(top, ["check-attr", "-z", "--stdin", "filter"], feed)
    if code != 0:
        outcome.notes.append(f"git check-attr failed: {error[:200]}")
        return None
    fields = attributes.split(b"\0")
    tracked = [fields[index].decode("utf-8", "surrogateescape") for index in range(0, len(fields) - 2, 3) if fields[index + 2] == b"lfs"]
    found: list[Pointer] = []
    for path in tracked:
        file = top / path
        try:
            info = file.lstat()
            if not file.is_file() or file.is_symlink() or info.st_size > POINTER_MAX_BYTES or info.st_size == 0:
                continue
            data = file.read_bytes()
        except OSError:
            continue
        parsed = parse_pointer(data)
        if parsed is not None:
            found.append(Pointer(path, parsed[0], parsed[1], data, parsed[2], *entries.get(path, ("100644", ""))))
    return found


def _filter_configured(top: Path, git: GitCall) -> bool:
    code, output, _ = git(top, ["config", "--get-regexp", r"^filter\.lfs\.(process|clean)$"], None)
    if code != 0:
        return False
    return any(len(line.split(maxsplit=1)) == 2 for line in output.decode("utf-8", "replace").splitlines())


def materialize(project: Path, cache: Path, *, git: GitCall = run_git, lfs: LfsCall = run_lfs,
                free: Callable[[Path], int] = diskspace.free_bytes, say: Callable[[str], None] | None = None) -> Outcome:
    """Turn the LFS pointer files of the Unity project folder `project` into their content; see the module text. `cache` is the
    shared cache root (`cache_root(work root)`). Never raises for a repository problem: the outcome says what happened."""
    outcome = Outcome(SKIPPED)
    project = Path(project)
    code, located, error = git(project, ["rev-parse", "--path-format=absolute", "--show-toplevel", "--absolute-git-dir",
                                         "--git-common-dir"], None)
    lines = located.decode("utf-8", "surrogateescape").splitlines() if code == 0 else []
    if len(lines) != 3:
        outcome.notes.append("not a git working tree" if code != 0 else "git did not name the repository folders")
        return outcome
    top, git_dir, common = (Path(line) for line in lines)
    if not checkout._uses_lfs(top, _text_runner(git)):  # noqa: SLF001 - the same attribute rule as the managed checkouts
        outcome.notes.append("no filter=lfs attribute")
        return outcome
    pointers = _scan(project, top, git, outcome)
    if pointers is None:
        outcome.pointers = 0
        outcome.code = CODE_GIT_FAILED
        outcome.reason = f"Git LFS preflight failed [{CODE_GIT_FAILED}]: " + "; ".join(outcome.notes)
        return outcome
    outcome.pointers = len(pointers)
    if not pointers:
        outcome.code = OK
        return outcome
    supported = [item for item in pointers if not item.extensions]
    if len(supported) != len(pointers):
        _fail(outcome, [item for item in pointers if item.extensions], CODE_UNSUPPORTED,
              "pointers with LFS extensions (ext-*) are not materialized by Valtrion; run `git lfs pull` in the project")
    if not supported:
        return _finish(outcome)
    if not _filter_configured(top, git):
        return _finish(_fail(outcome, supported, CODE_GIT_LFS_MISSING,
                             "git-lfs is not set up for this repository (no filter.lfs.process or filter.lfs.clean in the git config), so "
                             "materialized files would show as modified: install git-lfs and run `git lfs install`"))

    objects = cache / "objects"
    store = common / "lfs" / "objects"
    temp_dir = git_dir / TEMP_FOLDER
    _sweep_temp(temp_dir)
    by_oid: dict[str, list[Pointer]] = {}
    for item in supported:
        by_oid.setdefault(item.oid, []).append(item)

    def present(path: Path, size: int) -> bool:
        try:
            return path.is_file() and path.stat().st_size == size
        except OSError:
            return False

    # -- free space: the working-tree copies, the objects fetched into the store and the objects copied into the cache ---------------
    need: dict[int, list] = {}

    def add(path: Path, amount: int) -> None:
        device, probe = _volume(path)
        entry = need.setdefault(device, [probe, 0])
        entry[1] += amount

    for oid, items in by_oid.items():
        size = items[0].size
        add(top, size * len(items))
        in_cache, in_store = present(object_path(objects, oid), size), present(object_path(store, oid), size)
        if not in_store and not in_cache:
            add(store, size)
        if not in_cache:
            add(objects, size)
    for probe, amount in need.values():
        try:
            available = free(probe)
        except OSError:
            continue
        if available < amount + DISK_MARGIN_BYTES:
            return _finish(_fail(outcome, supported, CODE_DISK_SPACE,
                                 f"{available / 2**20:.0f} MiB free on the volume of {probe}, {(amount + DISK_MARGIN_BYTES) / 2**20:.0f} MiB "
                                 "needed for the LFS objects (with a 64 MiB margin)"))

    placed: list[Pointer] = []

    def place(item: Pointer, source: Path) -> str | None:
        """Copy the verified object over the pointer `item`; None on success, else the failure code (the note says why)."""
        target = top / item.path
        same = _volume(temp_dir)[0] == _volume(target.parent)[0]
        temp = _temp_name(temp_dir if same else target.parent, target.name)
        try:
            problem = _Copy.to_temp(source, item.oid, item.size, temp)
            if problem is not None:
                outcome.notes.append(f"hash mismatch: {problem}")
                return CODE_HASH_MISMATCH
            try:
                if target.read_bytes() != item.raw:  # changed since the scan: leave it alone
                    _unlink(temp)
                    outcome.notes.append(f"{item.path} changed while it was materialized; left as it is")
                    return CODE_WRITE_FAILED
                mode = target.stat().st_mode
            except OSError as error:
                _unlink(temp)
                outcome.notes.append(f"cannot read {target}: {error}")
                return CODE_WRITE_FAILED
            try:
                os.chmod(temp, mode & 0o7777)
            except OSError:
                pass
            os.replace(temp, target)
        except OSError as error:
            _unlink(temp)
            outcome.notes.append(f"cannot write {target}: {error}")
            return CODE_WRITE_FAILED
        except BaseException:  # an interrupt (Ctrl+C, a signal of `unity run`): no temporary file stays behind in the tree
            _unlink(temp)
            raise
        placed.append(item)
        outcome.materialized.append(item.path)
        return None

    def from_source(oid: str, source: Path, kind: str) -> tuple[bool, str | None]:
        """Place every pointer of `oid` from `source`; (all placed, failure code of the first that failed)."""
        for item in by_oid[oid]:
            if item.path in outcome.materialized:
                continue
            problem = place(item, source)
            if problem is not None:
                return False, problem
            outcome.sources[item.path] = kind
        return True, None

    pending: list[str] = []
    for oid, items in by_oid.items():
        size = items[0].size
        cached, stored = object_path(objects, oid), object_path(store, oid)
        done = False
        if present(cached, size):
            done, problem = from_source(oid, cached, "cache")
            if problem == CODE_HASH_MISMATCH:
                _unlink(cached)  # a damaged cache object is the cache's own: drop it and try the next source
                outcome.notes.append(f"removed the damaged cache object {oid[:12]}")
            elif not done:
                _fail(outcome, [item for item in items if item.path not in outcome.materialized], problem or CODE_WRITE_FAILED, "")
                continue
            if done:
                _link_or_copy(cached, stored, oid, size)  # so git-lfs's clean filter finds it and copies nothing
                continue
        if present(stored, size):
            done, problem = from_source(oid, stored, "repository")
            if done:
                _link_or_copy(stored, cached, oid, size)
                continue
            _fail(outcome, [item for item in items if item.path not in outcome.materialized], problem or CODE_WRITE_FAILED,
                  "the repository's LFS store holds a damaged object; run `git lfs fsck`" if problem == CODE_HASH_MISMATCH else "")
            continue
        pending.append(oid)

    if pending:
        waiting = [item for oid in pending for item in by_oid[oid]]
        code, version = lfs(top, ["version"])
        if code != 0:
            _fail(outcome, waiting, CODE_GIT_LFS_MISSING,
                  f"{len(pending)} object(s) are not on this host and git-lfs cannot fetch them ({' '.join(version.split())[:160]}); "
                  "install git-lfs")
        else:
            patterns = list(dict.fromkeys(include_pattern(item.path) for item in waiting))
            texts: list[str] = []
            for batch in _chunks(patterns):
                outcome.fetch_calls += 1
                if say is not None:
                    say(f"lfs: fetching {len(batch)} path(s) whose objects are not on this host")
                code, text = lfs(top, ["fetch", "--include", ",".join(batch), "--exclude", ""])
                if code != 0:
                    texts.append(text)
            for oid in pending:
                items = by_oid[oid]
                stored = object_path(store, oid)
                if not present(stored, items[0].size):
                    joined = " ".join(" ".join(texts).split())
                    failure = classify_fetch_error(joined) if joined else CODE_OBJECT_MISSING
                    _fail(outcome, items, failure,
                          f"git lfs fetch failed: {joined[:300]}" if joined else
                          f"the remote did not deliver the object {oid[:12]} (it is not on the remote)")
                    continue
                done, problem = from_source(oid, stored, "fetched")
                if done:
                    _link_or_copy(stored, object_path(objects, oid), oid, items[0].size)
                else:
                    _fail(outcome, [item for item in items if item.path not in outcome.materialized], problem or CODE_WRITE_FAILED,
                          "the fetched object does not match its pointer" if problem == CODE_HASH_MISMATCH else "")

    if placed:
        _verify_clean(top, git, placed, outcome)
    return _finish(outcome)


def _verify_clean(top: Path, git: GitCall, placed: list[Pointer], outcome: Outcome) -> None:
    """Refresh the index for the new files and make sure git sees none of them as modified; put back the pointer of any it does.

    The index still holds the stat data of the pointer, and git takes an entry whose recorded size differs from the file for modified
    without looking at the content. The entries are therefore written again with the same mode and blob (`--index-info`: what the
    index holds cannot change, only its stat data is cleared), so the refresh compares the content through the clean filter and records
    the new stat data when the filter gives back the committed pointer."""
    info = b"".join(f"{item.mode} {item.blob}\t".encode("ascii") + item.path.encode("utf-8", "surrogateescape") + b"\0"
                    for item in placed if item.blob)
    code, _, error = git(top, ["update-index", "-z", "--index-info"], info)
    if code != 0:
        outcome.notes.append(f"git update-index --index-info failed: {error[:200]}")
    git(top, ["update-index", "-q", "--refresh"], None)  # exit 1 only says some other entry needs an update
    # The whole tree: `status` compares the content (through the clean filter) also where the refresh could not write the index.
    code, output, error = git(top, ["status", "--porcelain", "-z", "--untracked-files=no", "--ignore-submodules=all"], None)
    if code != 0:
        outcome.notes.append(f"git status could not confirm the materialized files: {error[:200]}")
        dirty = {item.path for item in placed}
    else:
        dirty = {token[3:].decode("utf-8", "surrogateescape") for token in output.split(b"\0") if len(token) > 3}
    for item in placed:
        if item.path not in dirty:
            continue
        try:
            temp = _temp_name((top / item.path).parent, Path(item.path).name)
            temp.write_bytes(item.raw)
            os.replace(temp, top / item.path)
        except OSError as error:
            outcome.notes.append(f"cannot restore the pointer of {item.path}: {error}")
        outcome.materialized.remove(item.path)
        outcome.sources.pop(item.path, None)
        outcome.failed.append((item.path, CODE_STATUS_DIRTY))
    if any(code == CODE_STATUS_DIRTY for _, code in outcome.failed):
        git(top, ["update-index", "-q", "--refresh"], None)
        outcome.notes.append("git status showed materialized files as modified (the clean filter does not give back the pointer); "
                             "their pointers were restored")
