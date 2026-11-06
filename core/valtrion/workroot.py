"""The work root: where Valtrion builds, exports and checks out disposable data, outside folders the operating system protects.

macOS asks the owner for permission (TCC) whenever a process that is not already trusted reads or writes inside Documents,
Desktop, Downloads, iCloud Drive or a removable volume, and a one-shot background job has no stable identity to remember the
answer for. Repositories live in such folders and are never moved, so Valtrion keeps every process it starts out of them:

    <workRoot>/jobs/<jobId>/{checkout,build,export,derived-data,tmp}   everything one job creates, removed when the job ends
    <workRoot>/jobs/inbox-<id>/                                        files a remote caller sent for a job (`valtrion workroot inbox`)
    <workRoot>/mirrors/<project>.git                                   a bare clone that checkouts hang off (objects only)
    <workRoot>/scratch/<id>/                                           a temporary copy an agent asked for, with its marker (`scratch.py`)
    <workRoot>/tools/valtrion-<hash>/                                  the staged CLI that background jobs run
    <workRoot>/launchd/                                                job definitions and logs of macOS background jobs

Retained data (results, baselines, retained artifacts) lives under the artifacts root, never here. Defaults: macOS
`~/Library/Caches/com.valtrion/work` (not protected, not synced, excluded from backups; the data is disposable by
definition), Windows `%USERPROFILE%\\.valtrion\\work`, other systems `$XDG_CACHE_HOME/valtrion/work`.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Mapping, Sequence

from . import artifacts, compare, constants, core

WORK_ROOT_ENV = "GDV_WORK_ROOT"
GIT_TIMEOUT_SEC = 1800
STALE_JOB_DIRECTORY_SEC = 6 * 3600
INBOX_PREFIX = "inbox-"
#: The names `WorkRoots.inbox` makes; nothing else is ever removed as an inbox.
INBOX_NAME = re.compile(re.escape(INBOX_PREFIX) + r"[0-9a-f]{16}")
LAUNCHD_LABEL_PREFIX = "com.valtrion."
JOB_DIRECTORIES = ("checkout", "build", "export", "derived-data", "tmp")
_GIT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

#: Runs a command and returns (returncode, stdout+stderr). Replaced in tests.
Runner = Callable[[Sequence[str], "Path | None"], "tuple[int, str]"]


class WorkRootError(Exception):
    """A work-root operation failed (a git command, an unsafe path)."""


# -- location ---------------------------------------------------------------------------------------------------------------


def default_work_root(os_name: str | None = None, environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    environment = os.environ if environ is None else environ
    override = environment.get(WORK_ROOT_ENV)
    if override:
        return Path(override).expanduser()
    if environment.get(core.HOME_ENV):
        return Path(environment[core.HOME_ENV]) / "work"  # an isolated Valtrion home keeps its work root with it (tests, scratch runs)
    os_name = os_name or core.local_os()
    home = home or Path.home()
    if os_name == "macos":
        return home / "Library" / "Caches" / "com.valtrion" / "work"
    if os_name == "windows":
        # Beside the state home, not under %LOCALAPPDATA%: that folder is deep and a packaged (Store/MSIX) interpreter sees a private
        # copy of it, while the profile root is short, local, not synced and not redirected.
        return home / ".valtrion" / "work"
    cache = environment.get("XDG_CACHE_HOME")
    return (Path(cache) if cache else home / ".cache") / "valtrion" / "work"


def _parts(path: Path | str, os_name: str, resolve: bool) -> list[str]:
    text = os.path.realpath(path) if resolve else str(path)
    pure = PureWindowsPath(text) if os_name == "windows" else PurePosixPath(text.replace("\\", "/"))
    return [part.lower() for part in pure.parts]  # volumes are case-insensitive by default on macOS and Windows


def protected_location(path: Path, os_name: str | None = None, home: Path | None = None) -> str | None:
    """Why the operating system may prompt (or deny) for `path`, or None. A warning input for `valtrion doctor`, not a block: the
    owner may choose such a root on purpose. Pure string logic per `os_name`, so it can be tested on any host."""
    os_name = os_name or core.local_os()
    resolve = os_name == core.local_os()
    probe = _parts(path, os_name, resolve)
    base = _parts(home or Path.home(), os_name, resolve)

    def inside(*relative: str) -> bool:
        parent = base + [part.lower() for part in relative]
        return probe[: len(parent)] == parent

    if os_name == "macos":
        for name in ("Documents", "Desktop", "Downloads"):
            if inside(name):
                return f"inside the {name} folder (macOS asks for folder access, Files and Folders in Privacy & Security)"
        if inside("Library", "Mobile Documents") or inside("Library", "CloudStorage"):
            return "inside iCloud Drive or another cloud storage folder (macOS asks for access, and the data is synced)"
        if probe[:2] == ["/", "volumes"] and len(probe) > 2:
            return "on a removable or network volume (macOS asks for access to removable volumes)"
    elif os_name == "windows":
        if any(part.startswith("onedrive") for part in probe):
            return "inside OneDrive (synced; files may be online-only and Known Folder Move redirects Documents and Desktop)"
        for name in ("Documents", "Desktop", "Pictures"):
            if inside(name):
                return f"inside the {name} folder (Controlled folder access can block unknown programs there)"
    return None


#: Windows rejects paths of this many characters or more (MAX_PATH, counting the drive and the terminating NUL) in the programs
#: that have not opted in to long paths, and Unity is one of them.
WINDOWS_MAX_PATH = 260
#: The longest job id: a 16-character UTC stamp, `-`, a label of at most 40 characters, `-` and 8 characters of the commit
#: (`jobs.new_job_id`).
MAX_JOB_ID_LENGTH = 16 + 1 + 40 + 1 + 8
#: `\jobs\<jobId>\checkout\` below the work root, with the longest job id.
JOB_FOLDER_RESERVE = len("\\jobs\\") + MAX_JOB_ID_LENGTH + len("\\checkout\\")
#: ASSUMED length of the deepest file in a Unity project (Library and package cache entries nest deeply). Not measured; a project
#: that sits in a subfolder of its repository needs more.
DEEPEST_PROJECT_PATH = 134
#: MEASURED length of the deepest file after an Android IL2CPP build (Library\Bee\Android\Prj\IL2CPP\Gradle\...\.transforms\...): 247
#: in a project at the checkout root. With the work root and `\jobs\<id>\checkout\` in front it passes MAX_PATH at any root, so no
#: root length fixes an Android build while long paths are off.
ANDROID_DEEPEST_PROJECT_PATH = 247
#: Characters the check assumes a checkout needs below the work root: a Unity project at the checkout root.
CHECKOUT_PATH_RESERVE = JOB_FOLDER_RESERVE + DEEPEST_PROJECT_PATH
#: The longest work root (as given, in characters) that still leaves that reserve when long paths are off.
MAX_WORK_ROOT_LENGTH = WINDOWS_MAX_PATH - 1 - CHECKOUT_PATH_RESERVE
LONG_PATHS_KEY = r"SYSTEM\CurrentControlSet\Control\FileSystem"


def _read_long_paths_value() -> "int | None":
    """The raw `LongPathsEnabled` value of this Windows host, None when the value is absent. Raises OSError when the registry
    cannot be read. A seam for tests."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, LONG_PATHS_KEY) as key:
        try:
            return int(winreg.QueryValueEx(key, "LongPathsEnabled")[0])
        except FileNotFoundError:
            return None


def _is_windows_host() -> bool:
    return os.name == "nt"  # a seam for tests


def windows_long_paths_enabled(read: Callable[[], "int | None"] = _read_long_paths_value,
                               is_windows: Callable[[], bool] = _is_windows_host) -> "bool | None":
    """Whether Windows long paths are switched on (`LongPathsEnabled` under HKLM\\SYSTEM\\CurrentControlSet\\Control\\FileSystem).
    False when the value is absent or 0 (the Windows default), None when this is not Windows or the registry cannot be read. On
    is necessary but not sufficient: git for Windows needs `core.longpaths` and each tool must opt in."""
    if not is_windows():
        return None
    try:
        value = read()
    except (OSError, ImportError):
        return None
    return bool(value)


def path_length_warning(path: Path, os_name: str | None = None, long_paths_enabled: "bool | None" = None,
                        job_id: str | None = None, android: bool = False) -> str | None:
    """Why a Unity project checkout under `path` may not fit the Windows path limit, or None. Only on Windows, only when long paths
    are known to be off (`long_paths_enabled` is False; True and None, an unreadable setting, stay silent). A warning input for
    `valtrion doctor` and for job start. Pure: the length of the path AS GIVEN is counted (a subst drive or junction that shortens
    it counts), and the setting is passed in. With `job_id` the reserve uses that job's real folder name instead of the longest
    possible one, so a job start does not warn for an id shorter than the worst case. With `android` (an Android build is
    possible or planned) the answer does not depend on the root: the measured deepest Android build file cannot fit, so it says to
    turn long paths on."""
    os_name = os_name or core.local_os()
    if os_name != "windows" or long_paths_enabled is not False:
        return None
    if android:
        return (f"cannot hold a Unity Android build while Windows long paths are off: an Android (Gradle/IL2CPP) build writes files "
                f"up to {ANDROID_DEEPEST_PROJECT_PATH} characters below the project (measured), which cannot fit in the "
                f"{WINDOWS_MAX_PATH}-character path limit under any work root; turn on LongPathsEnabled (HKLM\\SYSTEM\\CurrentControlSet"
                "\\Control\\FileSystem; needs an administrator and a restart) and run `git config --global core.longpaths true`")
    text = str(path).replace("/", "\\")
    if text.startswith("\\\\?\\UNC\\"):  # the extended-length prefix is not part of the path the limit counts
        text = "\\\\" + text[len("\\\\?\\UNC\\"):]
    elif text.startswith("\\\\?\\"):
        text = text[len("\\\\?\\"):]
    limit = MAX_WORK_ROOT_LENGTH
    if job_id is not None:
        limit = WINDOWS_MAX_PATH - 1 - (len("\\jobs\\") + len(job_id) + len("\\checkout\\") + DEEPEST_PROJECT_PATH)
    if len(text) <= limit:
        return None
    return (f"is {len(text)} characters long and Windows long paths are off: a Unity project checkout under it can pass the "
            f"{WINDOWS_MAX_PATH}-character path limit (this check assumes the root should stay within {limit} characters); "
            "set `workRoot` in host.json or GDV_WORK_ROOT to a short absolute path")


def start_warnings(path: Path, job_id: str, os_name: str | None = None, android: bool = False,
                   long_paths_enabled: "Callable[[], bool | None]" = windows_long_paths_enabled) -> list[str]:
    """What `valtrion doctor` would say about the work root, for one job that is about to start: a virtualized location, or a path
    too long for this job's checkout. Empty off Windows. The same wording as doctor, prefixed with the root, so the owner finds one
    fix in both places."""
    os_name = os_name or core.local_os()
    if os_name != "windows":
        return []
    reason = virtualized_location(path, os_name) or path_length_warning(path, os_name, long_paths_enabled(), job_id, android)
    return [f"work root {path} {reason}"] if reason else []


def longest_path(base: Path) -> int:
    """Characters of the longest file path under `base`, counted as given (drive and `base` included), 0 for an empty tree. The
    walk uses the extended-length form on Windows, so it also reaches paths past the limit; an unreadable folder is skipped."""
    start = str(base)
    prefixed = "\\\\?\\" + start if os.name == "nt" and not start.startswith("\\\\") else start
    cut = len(prefixed) - len(start)
    longest = 0
    for folder, _, files in os.walk(prefixed):
        for name in files:
            longest = max(longest, len(folder) - cut + 1 + len(name))
    return longest


def measured_path_warning(length: int, os_name: str | None = None, long_paths_enabled: "bool | None" = None) -> str | None:
    """Why a checkout whose longest file path is `length` characters (measured, see `longest_path`) fails the Windows path limit,
    or None. Only on Windows with long paths known to be off. Unlike `path_length_warning` this rests on a measurement of the real
    tree, so it names the exact overflow."""
    os_name = os_name or core.local_os()
    if os_name != "windows" or long_paths_enabled is not False or length < WINDOWS_MAX_PATH:
        return None
    return (f"the longest file path in the checkout is {length} characters and Windows long paths are off: the limit is "
            f"{WINDOWS_MAX_PATH - 1}, so {length - WINDOWS_MAX_PATH + 1} characters too many; use a work root that is at least that much "
            "shorter (`workRoot` in host.json or GDV_WORK_ROOT), or turn on LongPathsEnabled (HKLM\\SYSTEM\\CurrentControlSet\\Control\\FileSystem; "
            "needs an administrator and a restart) and `git config --global core.longpaths true`")


def checkout_warnings(checkout: Path, os_name: str | None = None,
                      long_paths_enabled: "Callable[[], bool | None]" = windows_long_paths_enabled,
                      measure: Callable[[Path], int] = longest_path) -> list[str]:
    """Measure `checkout` and report `measured_path_warning`. Nothing is walked unless this is Windows with long paths off."""
    os_name = os_name or core.local_os()
    if os_name != "windows" or long_paths_enabled() is not False:
        return []
    reason = measured_path_warning(measure(checkout), os_name, False)
    return [reason] if reason else []


# -- virtualized AppData ----------------------------------------------------------------------------------------------------


def packaged_interpreter(executable: str | None = None, prefix: str | None = None) -> bool:
    """Whether the running Python is a packaged app (a Store or MSIX install): its executable or base prefix lies under
    `\\WindowsApps\\` or `\\AppData\\Local\\Packages\\<name>\\`. Such a process sees a private copy of AppData."""
    for text in (sys.executable if executable is None else executable, sys.base_prefix if prefix is None else prefix):
        parts = [part.lower() for part in PureWindowsPath(text.replace("/", "\\")).parts]
        if "windowsapps" in parts or _find(parts, ["appdata", "local", "packages", None]) >= 0:
            return True
    return False


def _find(parts: list[str], pattern: list) -> int:
    """Index where `pattern` (None matches any one part) occurs in `parts` as consecutive parts, or -1."""
    for index in range(len(parts) - len(pattern) + 1):
        if all(want is None or parts[index + offset] == want for offset, want in enumerate(pattern)):
            return index
    return -1


def virtualized_location(path: Path, os_name: str | None = None, environ: Mapping[str, str] | None = None, home: Path | None = None,
                         packaged: "bool | None" = None) -> str | None:
    """Why `path` may not be the folder other programs see, or None (Windows only). Two independent signals:

    - its real path (resolved only on the host it belongs to) or the path as given runs through
      `AppData\\Local\\Packages\\<name>\\LocalCache`, a packaged app's private copy of AppData;
    - the running Python is a packaged app (`packaged`, default: `packaged_interpreter()`) and the path lies under the nominal
      `%LOCALAPPDATA%`, which that process sees redirected even when it does not resolve the redirect or the folder does not exist yet.

    Neither is exhaustive: both depend on how the interpreter resolves paths. A warning input for `valtrion doctor`."""
    os_name = os_name or core.local_os()
    if os_name != "windows":
        return None
    resolve = os_name == core.local_os()
    cache = ["appdata", "local", "packages", None, "localcache"]
    if _find(_parts(path, os_name, resolve), cache) >= 0 or _find(_parts(path, os_name, False), cache) >= 0:
        return ("in a packaged app's private copy of AppData (AppData\\Local\\Packages\\<name>\\LocalCache): a path that only the "
                "packaged program sees, so Unity and git started from elsewhere find nothing or another copy")  # signal (a), seen
    environment = os.environ if environ is None else environ
    if packaged is None:
        packaged = packaged_interpreter()
    if packaged and not (resolve and os.path.exists(path)):  # an existing root was resolved above: that answer beats the assumption
        local = Path(environment.get("LOCALAPPDATA") or (home or Path.home()) / "AppData" / "Local")
        base = _parts(local, os_name, False)
        if _parts(path, os_name, False)[: len(base)] == base:
            return ("under %LOCALAPPDATA% while this Python is a packaged app: such a process may see a private copy of AppData there "
                    "(not checked: the folder does not exist yet), and then Unity and git started from elsewhere find nothing or another copy")
    return None


@dataclass(frozen=True)
class JobDirs:
    """The directories of one job. Names are fixed so any tool can derive them from `GDV_JOB_DIR`."""

    base: Path

    @property
    def checkout(self) -> Path:
        return self.base / "checkout"

    @property
    def build(self) -> Path:
        return self.base / "build"

    @property
    def export(self) -> Path:
        return self.base / "export"

    @property
    def derived_data(self) -> Path:
        return self.base / "derived-data"

    @property
    def tmp(self) -> Path:
        return self.base / "tmp"

    def create(self) -> None:
        # `checkout` is left to git, which creates it; the others exist so a tool can write into them at once.
        for name in JOB_DIRECTORIES:
            if name != "checkout":
                (self.base / name).mkdir(parents=True, exist_ok=True)

    def environment(self, root: "WorkRoots", run_directory: Path | None = None) -> dict:
        values = {
            "GDV_WORK_ROOT": str(root.root), "GDV_JOB_DIR": str(self.base), "GDV_JOB_CHECKOUT": str(self.checkout),
            "GDV_JOB_BUILD": str(self.build), "GDV_JOB_EXPORT": str(self.export), "GDV_JOB_DERIVED_DATA": str(self.derived_data),
            "GDV_JOB_TMP": str(self.tmp), "GDV_JOB_ID": self.base.name}
        if run_directory is not None:
            values["GDV_RUN_DIR"] = str(run_directory)  # retained results and diagnostics: write logs here, not under the job dir
        return values


@dataclass(frozen=True)
class WorkRoots:
    root: Path
    #: Where the staged CLI copies live; the work root's own `tools` when None. The host keeps them in `<valtrion home>/tools`,
    #: outside the disposable (and, on macOS, purgeable) work root.
    tools_root: Path | None = None

    @property
    def jobs(self) -> Path:
        return self.root / "jobs"

    @property
    def mirrors(self) -> Path:
        return self.root / "mirrors"

    @property
    def tools(self) -> Path:
        return self.tools_root if self.tools_root is not None else self.root / "tools"

    @property
    def launchd(self) -> Path:
        return self.root / "launchd"

    @property
    def scratch(self) -> Path:
        """Temporary copies an agent asked for (`valtrion workroot scratch create`), one folder with a marker each (`scratch.py`)."""
        return self.root / "scratch"

    def job(self, job_id: str) -> JobDirs:
        return JobDirs(self.jobs / core.safe_segment(job_id, "job", 120))

    def inbox(self) -> Path:
        """A new private folder `<jobs>/inbox-<random>` (owner-only where the platform supports it) for the files a remote caller sends
        before it starts a job. It sits beside the job directories, so the sweep removes it once nothing has touched it for hours."""
        self.jobs.mkdir(parents=True, exist_ok=True)
        folder = self.jobs / f"{INBOX_PREFIX}{uuid.uuid4().hex[:16]}"
        folder.mkdir(mode=0o700)
        return folder

    def mirror(self, project: str) -> Path:
        return self.mirrors / f"{core.safe_segment(project, 'project', 80)}.git"


# -- staged CLI -------------------------------------------------------------------------------------------------------------


def _schema_folder(cli_root: Path) -> Path:
    packed = cli_root / "valtrion" / "_schemas"  # an installed wheel
    return packed if packed.is_dir() else cli_root.parent / "contracts" / "schemas"  # a checkout


def _runtime_files(cli_root: Path) -> list[tuple[Path, Path]]:
    """(source, path inside the staged copy) of the package modules and the contract schemas, which the copy keeps in `_schemas`."""
    files = [(path, path.relative_to(cli_root)) for path in sorted((cli_root / "valtrion").rglob("*.py"))]
    files.extend((path, Path("valtrion") / "_schemas" / path.name) for path in sorted(_schema_folder(cli_root).glob("*.json")))
    return [(path, target) for path, target in files if "__pycache__" not in path.parts]


def runtime_hash(cli_root: Path) -> str:
    digest = hashlib.sha256()
    for path, target in _runtime_files(cli_root):
        digest.update(target.as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()[:12]


def stage_runtime(roots: WorkRoots, cli_root: Path) -> Path:
    """Copy the CLI to `<workRoot>/tools/valtrion-<hash>` (once per content) and return that root. A background job then runs from
    here and never reads the folder that holds the original, which may be protected. Idempotent and safe against two starts at
    once: a copy is built beside the target and renamed into place."""
    target = roots.tools / f"valtrion-{runtime_hash(cli_root)}"
    if (target / "valtrion" / "__init__.py").is_file():
        try:
            os.utime(target)  # marks it in use, so the sweep only prunes copies no recent job has used
        except OSError:
            pass
        return target
    roots.tools.mkdir(parents=True, exist_ok=True)
    scratch = roots.tools / f".stage-{os.getpid()}-{time.monotonic_ns()}"
    try:
        for path, relative in _runtime_files(cli_root):
            destination = scratch / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        try:
            os.replace(scratch, target)
        except OSError:
            if not (target / "valtrion" / "__init__.py").is_file():  # another start won the race: its copy is identical
                raise
    finally:
        if scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)
    return target


# -- git: a mirror and disposable checkouts ---------------------------------------------------------------------------------


def run_git(argv: Sequence[str], cwd: Path | None = None) -> tuple[int, str]:
    environment = compare.git_environment()  # no inherited GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE, no prompt
    environment["PATH"] = environment.get("PATH", "") + os.pathsep + _GIT_PATH  # git-lfs lives in the Homebrew prefix on macOS
    try:
        done = subprocess.run(
            ["git", *argv], cwd=cwd, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=GIT_TIMEOUT_SEC, check=False, stdin=subprocess.DEVNULL, creationflags=constants.NO_WINDOW)
    except subprocess.TimeoutExpired:
        return 124, f"git {argv[0]} timed out after {GIT_TIMEOUT_SEC} s"
    except OSError as error:
        return 127, str(error)
    return done.returncode, (done.stdout + done.stderr).strip()


def _git(run: Runner, argv: Sequence[str], cwd: Path | None = None) -> str:
    code, output = run(argv, cwd)
    if code != 0:
        raise WorkRootError(f"git {' '.join(argv[:3])} failed ({code}): {output[:300]}")
    return output


def ensure_mirror(roots: WorkRoots, project: str, source: str, sha: str, run: Runner = run_git) -> Path:
    """A bare repository under the work root that holds `sha`. `source` is a URL (preferred: no folder is read) or a repository
    path; only the one `git fetch` reads it."""
    mirror = roots.mirror(project)
    if not (mirror / "HEAD").is_file():
        mirror.parent.mkdir(parents=True, exist_ok=True)
        _git(run, ["init", "--bare", "--quiet", str(mirror)])
    code, _ = run(["cat-file", "-e", f"{sha}^{{commit}}"], mirror)
    if code != 0:
        _git(run, ["fetch", "--no-tags", "--quiet", source, sha], mirror)
    return mirror


def record_default_branch(mirror: Path, source: str, sha: str, run: Runner = run_git) -> str | None:
    """Record the source's default branch as `origin/<default>` in the mirror, so a checkout can tell whether its commit is on it
    (`valtrion baseline promote` asks that). Skipped when the recorded ref already holds `sha`. Returns the branch name, or None when the
    source names no default branch or cannot be read: the checkout still works, and promotion then refuses by itself."""
    code, listing = run(["ls-remote", "--symref", source, "HEAD"], mirror)
    if code != 0:
        return None
    branch = None
    for line in listing.splitlines():
        if line.startswith("ref: refs/heads/"):
            branch = line[len("ref: refs/heads/"):].split("\t", 1)[0].strip()
            break
    if not branch:
        return None
    tracking = f"refs/remotes/origin/{branch}"
    code, _ = run(["merge-base", "--is-ancestor", sha, tracking], mirror)
    if code != 0:
        code, _ = run(["fetch", "--no-tags", "--quiet", source, f"+refs/heads/{branch}:{tracking}"], mirror)
        if code != 0:
            return None
    return branch


def add_checkout(roots: WorkRoots, project: str, source: str, sha: str, job_id: str, run: Runner = run_git) -> Path:
    """A detached checkout of `sha` in the job's `checkout` directory, hanging off the mirror (so git reads only the work root)."""
    check_sha(sha)
    dirs = roots.job(job_id)
    if dirs.checkout.exists():
        raise WorkRootError(f"{dirs.checkout} already exists")
    return add_worktree(roots, project, source, sha, dirs.checkout, run)


def check_sha(sha: str) -> None:
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise WorkRootError("the candidate must be a full lowercase 40-character commit hash")


def add_worktree(roots: WorkRoots, project: str, source: str, sha: str, target: Path, run: Runner = run_git) -> Path:
    """A detached checkout of `sha` at `target` (a folder under the work root that does not exist yet), hanging off the project's
    mirror, with its Git LFS files restored. Used for a job's checkout and for a scratch copy (`scratch.create`)."""
    check_sha(sha)
    mirror = ensure_mirror(roots, project, source, sha, run)
    record_default_branch(mirror, source, sha, run)
    target.parent.mkdir(parents=True, exist_ok=True)
    # The mirror has no LFS objects yet, so the smudge filter is switched off for the checkout and the objects are fetched after.
    _git(run, ["-c", "filter.lfs.smudge=", "-c", "filter.lfs.process=", "-c", "filter.lfs.required=false",
               "worktree", "add", "--detach", "--quiet", str(target), sha], mirror)
    # `lfs ls-files` honours attributes at any level (nested .gitattributes, info/attributes, global); it fails when git-lfs is
    # not installed, and then the checkout has no LFS files to restore.
    code, tracked = run(["lfs", "ls-files", "--name-only"], target)
    if code != 0:
        # No usable git-lfs: fine for a tree without LFS rules, but silently leaving pointer files in an LFS tree would make the
        # build compile text instead of assets.
        declared, _ = run(["grep", "-l", "filter=lfs", "--", ".gitattributes", ":(glob)**/.gitattributes"], target)
        if declared == 0:
            raise WorkRootError(f"the commit uses Git LFS but git-lfs is not usable on this host: {tracked[:200]}")
    elif tracked.strip():
        _git(run, ["lfs", "fetch", source, sha], mirror)
        _git(run, ["lfs", "checkout"], target)
    return target


def prune_mirrors(roots: WorkRoots, run: Runner = run_git) -> None:
    if not roots.mirrors.is_dir():
        return
    for mirror in sorted(roots.mirrors.glob("*.git")):
        run(["worktree", "prune"], mirror)


# -- cleanup ----------------------------------------------------------------------------------------------------------------


def _inside_jobs(roots: WorkRoots, path: Path) -> bool:
    try:
        path.resolve().relative_to(roots.jobs.resolve())
        return path.resolve() != roots.jobs.resolve()
    except ValueError:
        return False


def cleanup_job(roots: WorkRoots, job_id: str, run: Runner = run_git, inbox: str | None = None) -> list[str]:
    """Remove everything the job created under the work root (and the inbox a caller sent its files to, when the job was started
    with one), then forget its checkout in the mirrors. The run's results and logs live under the artifacts root and are not
    touched. Returns what was removed; an unremovable path is raised."""
    removed = cleanup_inbox(roots, inbox) if inbox else []
    dirs = roots.job(job_id)
    if not dirs.base.exists() and not dirs.base.is_symlink():
        return removed
    if not _inside_jobs(roots, dirs.base):
        raise WorkRootError(f"{dirs.base} is not inside the work root's jobs folder")
    artifacts._remove(dirs.base)
    prune_mirrors(roots, run)
    return [*removed, str(dirs.base)]


def cleanup_inbox(roots: WorkRoots, name: str) -> list[str]:
    """Remove the inbox folder `name` (as `WorkRoots.inbox` made it) that a job was started with. Returns what was removed."""
    if not INBOX_NAME.fullmatch(name):
        raise WorkRootError(f"{name!r} is not an inbox folder name")
    folder = roots.jobs / name
    if not folder.is_dir() or not _inside_jobs(roots, folder):
        return []
    artifacts._remove(folder)
    return [str(folder)]


def sweep(roots: WorkRoots, active_jobs: Sequence[str], max_age_sec: float = STALE_JOB_DIRECTORY_SEC, dry_run: bool = False,
          clock: Callable[[], float] = time.time, run: Runner = run_git,
          keep: Callable[[str], "str | None"] | None = None) -> list[str]:
    """Remove job directories that no active job owns and that nothing has written to for `max_age_sec` (a runner that died before
    its own cleanup). The mirrors are kept: they are the cache that makes the next checkout cheap."""
    removed: list[str] = []
    active = {core.safe_segment(item, "job", 120) for item in active_jobs}
    cutoff = clock() - max_age_sec
    if not roots.jobs.is_dir():
        return [*_sweep_launchd_files(roots, active, cutoff, dry_run), *_sweep_tools(roots, active, cutoff, dry_run)]
    for folder in sorted(p for p in roots.jobs.iterdir() if p.is_dir()):
        if folder.name in active:
            continue
        if keep is not None and keep(folder.name):  # retained, protected or otherwise kept on purpose: never swept
            continue
        if newest_mtime(folder) > cutoff:
            continue
        if not dry_run:
            artifacts._remove(folder)
        removed.append(str(folder))
    if removed and not dry_run:
        prune_mirrors(roots, run)
    removed.extend(_sweep_launchd_files(roots, active, cutoff, dry_run))
    removed.extend(_sweep_tools(roots, active, cutoff, dry_run))
    return removed


def _sweep_tools(roots: WorkRoots, active: set, cutoff: float, dry_run: bool) -> list[str]:
    """Staged CLI copies nothing has used lately and half-made copies of a crashed stage. Nothing is pruned while a job is active:
    its runner may still import from its copy."""
    removed: list[str] = []
    if active or not roots.tools.is_dir():
        return removed
    for item in sorted(p for p in roots.tools.iterdir() if p.is_dir() and (p.name.startswith("valtrion-") or p.name.startswith(".stage-"))):
        if newest_mtime(item) > cutoff:
            continue
        if not dry_run:
            artifacts._remove(item)
        removed.append(str(item))
    return removed


def _sweep_launchd_files(roots: WorkRoots, active: set, cutoff: float, dry_run: bool) -> list[str]:
    """Job definitions and logs of macOS background jobs whose runner died before it could retire them."""
    removed: list[str] = []
    if not roots.launchd.is_dir():
        return removed
    for item in sorted(p for p in roots.launchd.iterdir() if p.is_file() and p.suffix in (".plist", ".log")):
        job_id = item.name[len(LAUNCHD_LABEL_PREFIX):-len(item.suffix)] if item.name.startswith(LAUNCHD_LABEL_PREFIX) else None
        if job_id is None or job_id in active or newest_mtime(item) > cutoff:
            continue
        if not dry_run:
            item.unlink()
        removed.append(str(item))
    return removed


def newest_mtime(folder: Path) -> float:
    """Newest modification time of a folder and its direct children; an entry that vanishes meanwhile is ignored."""
    newest = 0.0
    try:
        entries = [folder, *folder.iterdir()] if folder.is_dir() and not folder.is_symlink() else [folder]
    except OSError:
        entries = [folder]
    for entry in entries:
        try:
            newest = max(newest, entry.lstat().st_mtime)
        except OSError:
            continue
    return newest


def stale_job_directories(roots: WorkRoots, max_age_sec: float = STALE_JOB_DIRECTORY_SEC,
                          clock: Callable[[], float] = time.time) -> list[Path]:
    """Job directories untouched for `max_age_sec` (an abandoned one, or a job that is merely quiet: doctor only warns)."""
    if not roots.jobs.is_dir():
        return []
    cutoff = clock() - max_age_sec
    return [folder for folder in sorted(roots.jobs.iterdir())
            if folder.is_dir() and newest_mtime(folder) <= cutoff]


def usage(roots: WorkRoots) -> dict:
    """Size facts for `valtrion doctor`: how many job directories exist and how big the work root is."""
    def size(path: Path) -> int:
        total = 0
        for folder, _, files in os.walk(path):
            for name in files:
                try:
                    total += os.lstat(os.path.join(folder, name)).st_size
                except OSError:
                    pass
        return total

    jobs = [p for p in roots.jobs.iterdir() if p.is_dir()] if roots.jobs.is_dir() else []
    scratch = [p for p in roots.scratch.iterdir() if p.is_dir()] if roots.scratch.is_dir() else []
    return {"jobDirectories": len(jobs), "mirrorCount": len(list(roots.mirrors.glob("*.git"))) if roots.mirrors.is_dir() else 0,
            "scratchDirectories": len(scratch), "bytes": size(roots.root) if roots.root.is_dir() else 0}
