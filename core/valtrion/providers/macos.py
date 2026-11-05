"""macOS adapter: builds a validation player with Unity batchmode, signs it, and runs it as a supervised process on the Mac itself.

The target is the Mac that runs the job, so a Mac caller runs locally and any other caller reaches the Mac over SSH through the
host resolver. A job that signs runs in the GUI launchd domain (`--domain gui`); this adapter only runs the signing tool.

* Isolation: a macOS player keeps its data in `~/Library/Application Support/<bundle identifier>`, so the validation player is built
  with its own identifier (the application's plus `.gdv`) and its own company and product names. `install` refuses a bundle whose
  identifier does not end in `.gdv`, and only such a folder is ever cleared.
* Launch: the binary inside the bundle (`<App>.app/Contents/MacOS/<binary>`) is started directly with `-gdv-plan` and `-logFile`, in
  its own session, so its whole process group is killed on terminate. It is never started through `open`.
* Permissions: quarantine and TCC are never changed. A quarantined bundle, or an application that dies on a missing permission, is
  NEEDS_PERMISSION with the action the owner takes.
* Cleanup: the build folder is registered as temporary by the pipeline; after a passing run the isolated data folder is removed by
  `cleanup_run_data` once the results were collected, and again before an install, so a run never starts from an earlier run's data.
  A failed or interrupted run keeps its data instead: `retain_run_data` writes `retained.json` (what failed, when, which holder) and
  moves the folder aside as `<identifier>.gdv.retained-<utc>`; no later run clears it, and the task that started the run removes it
  after review.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .. import config, constants, core, doctor, retention, supervise
from ..adapter import Capabilities, DeviceHealth, DeviceInfo, PlatformAdapter, StepOutcome
from . import register

PLATFORM = "macos"
COMPANY = "Valtrion-Validation"
VALIDATION_SUFFIX = ".gdv"
BUILD_METHOD_ENV = "GDV_MACOS_BUILD_METHOD"
SIGN_SCRIPT_ENV = "GDV_MACOS_SIGN_SCRIPT"
SIGN_SCRIPT_FOLDER = Path("tools") / "build"  # without `signScript`, the first `sign-macos*.py` of this folder in the project or above it
PROFILES_FILE = Path("validation") / "device" / "profiles.json"
BUILD_TIMEOUT_SEC = 3600.0
BUILD_STALL_SEC = 600.0
SETTLE_SEC = 3.0
LOG_SCAN_BYTES = 1_000_000
KILL_GRACE_SEC = 5.0
TOOL_TIMEOUT_SEC = 120.0
_APP_DATA = Path("Library") / "Application Support"

#: Crash reasons of a process that touched a privacy-protected resource without permission; applied only to a process that exited.
_PERMISSION_TEXT = re.compile(
    r"(attempted to access privacy-sensitive data without a usage description|"
    r"not authorized to (use|access) (the )?(camera|microphone|photos|location)|"
    r"authorization (status )?(is )?(denied|not determined) for (the )?(camera|microphone))", re.IGNORECASE)
_LICENSE = re.compile(r"(No valid Unity Editor license|Unity has not been activated|no (valid )?license (was )?found)", re.IGNORECASE)
_SIGNING_UI = re.compile(r"(errSecInternalComponent|User interaction is not allowed|The specified item could not be found in the keychain)")
_EDITOR_BUSY = re.compile(r"(another Unity instance is running with this project open|It looks like another Unity instance)", re.IGNORECASE)

Runner = Callable[..., supervise.BoundedResult]


@dataclass(frozen=True)
class Identity:
    company: str
    product: str


def identity_for(project: Path) -> Identity:
    """The validation names of a project: stable per project, never the application's own."""
    name = core.safe_segment(project.name.replace(" ", "-"), "project", 60)
    return Identity(COMPANY, f"{name}-gdv")


def data_root_for(bundle_id: str, home: Path | None = None) -> Path:
    """Where a macOS Unity player keeps `Application.persistentDataPath`: `~/Library/Application Support/<bundle identifier>`."""
    return (home or Path.home()) / _APP_DATA / bundle_id


def _relative_inside(root: Path, relative: str) -> Path | None:
    """`relative` resolved below `root`, or None when it is absolute or leaves the root."""
    if not relative or relative.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", relative):
        return None
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError:
        return None
    return candidate


def permission_problem(text: str) -> str | None:
    """The line that shows the process died or stopped on a macOS privacy permission, if any."""
    for line in text.splitlines():
        if _PERMISSION_TEXT.search(line):
            return line.strip()[:240]
    return None


def _tail(path: Path, limit: int = 2000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", "replace")


def load_profile(project: Path, profile: str = "Validation") -> dict:
    """The macOS section of a build profile in `<project>/validation/device/profiles.json` (`gdv.profiles/1`:
    `{"profiles": {"<name>": {"<platform>": {...}}}}`)."""
    try:
        document = json.loads((project / PROFILES_FILE).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    profiles = document.get("profiles") if isinstance(document, dict) else None
    named = profiles.get(profile) if isinstance(profiles, dict) else None
    section = named.get(PLATFORM) if isinstance(named, dict) else None
    return section if isinstance(section, dict) else {}


def find_build_method(project: Path, environ: Mapping[str, str], profile: str = "Validation") -> str | None:
    if environ.get(BUILD_METHOD_ENV):
        return environ[BUILD_METHOD_ENV]
    method = load_profile(project, profile).get("executeMethod")
    return method if isinstance(method, str) and method else None


def find_sign_script(project: Path, environ: Mapping[str, str], profile: str = "Validation") -> Path | None:
    configured = environ.get(SIGN_SCRIPT_ENV) or load_profile(project, profile).get("signScript")
    if configured:
        path = Path(configured) if Path(configured).is_absolute() else project / configured
        return path if path.is_file() else None
    for folder in [project.resolve(), *project.resolve().parents]:
        found = sorted((folder / SIGN_SCRIPT_FOLDER).glob("sign-macos*.py")) if (folder / SIGN_SCRIPT_FOLDER).is_dir() else []
        if found:
            return found[0]
    return None


class MacOSAdapter(PlatformAdapter):
    platform = PLATFORM

    def __init__(
            self, run: Runner | None = None, environ: Mapping[str, str] | None = None, home: Path | None = None,
            sleep: Callable[[float], None] = time.sleep, settle_sec: float = SETTLE_SEC, editors: Callable[[], list[dict]] | None = None,
            state_dir: Path | None = None, spawn: Callable | None = None, darwin: bool | None = None) -> None:
        self._darwin = sys.platform == "darwin" if darwin is None else darwin
        self._spawn = spawn or supervise.spawn
        self._run = run or supervise.run_bounded
        self._environ = os.environ if environ is None else environ
        self._home = home
        self._sleep = sleep
        self._settle_sec = settle_sec
        self._editors = editors or (lambda: doctor.find_editors("macos"))
        self._state_dir = state_dir
        self._tree: supervise.ProcessTree | None = None
        self._process = None
        self._identity: Identity | None = None

    # -- state ---------------------------------------------------------------------------------------------------------------

    @property
    def state_dir(self) -> Path:
        return self._state_dir or core.valtrion_home() / "macos"

    def _installed_path(self) -> Path:
        return self.state_dir / "installed.json"

    def _running_path(self) -> Path:
        return self.state_dir / "running.json"

    def _read_state(self, path: Path) -> dict | None:
        try:
            document = core.read_json(path, attempts=2)
        except (OSError, ValueError):
            return None
        return document if isinstance(document, dict) else None

    def _installed(self) -> dict | None:
        return self._read_state(self._installed_path())

    def _data_root(self) -> Path | None:
        record = self._installed()
        if not record:
            return None
        return Path(record["dataRoot"])

    # -- capabilities and discovery ------------------------------------------------------------------------------------------

    def probe(self) -> Capabilities:
        available = ["launch", "terminate", "push", "pull", "health", "screenshot"]
        blocked: dict[str, str] = {}
        if not self._darwin:
            return Capabilities(PLATFORM, (), {name: "the macOS adapter runs on a Mac" for name in ("build", *available)})
        editors = [e for e in self._editors() if "macos" in e["modules"]]
        if editors:
            available.append("build")
        else:
            blocked["build"] = "no Unity editor with the macOS build support module was found"
        if not shutil.which("codesign"):
            blocked["sign"] = "codesign is not available"
        else:
            available.append("sign")
        return Capabilities(PLATFORM, tuple(available), blocked)

    def _output(self, argv: Sequence[str], timeout: float = 20.0) -> tuple[int, str]:
        result = self._run(list(argv), timeout, stall_sec=timeout)
        return (result.returncode if result.returncode is not None else 1), result.output.strip()

    def discover(self) -> Sequence[DeviceInfo]:
        if not self._darwin:
            return []
        _, brand = self._output(["sysctl", "-n", "machdep.cpu.brand_string"])
        _, model = self._output(["sysctl", "-n", "hw.model"])
        _, version = self._output(["sw_vers", "-productVersion"])
        chip = re.sub(r"[^a-z0-9]+", "-", (brand or "mac").lower()).strip("-") or "mac"
        return [DeviceInfo(
            id=f"macos:{core.local_host_id()}", platform=PLATFORM, device_class=f"mac-{chip}", evidence=constants.EVIDENCE_HOST,
            model=model, os_version=version, capabilities=("desktop", "screenshot"), exclusive=True)]

    def health(self, device: DeviceInfo) -> DeviceHealth:
        notes: list[str] = []
        locked: bool | None = None
        code, output = self._output(["ioreg", "-n", "Root", "-d1", "-a"])
        if code == 0 and output:
            try:
                root = plistlib.loads(output.encode("utf-8"))
                root = root[0] if isinstance(root, list) and root else root  # `ioreg -a` prints an array of registry entries
                users = root.get("IOConsoleUsers") or []
                if users:
                    locked = bool(users[0].get("CGSSessionScreenIsLocked", False))
                else:
                    notes.append("no console user is logged in; a GUI player needs a logged-in session")
            except Exception:  # noqa: BLE001 - unparsable output is recorded as unknown, never guessed
                notes.append("the lock state could not be read")
        thermal: str | None = None
        code, output = self._output(["pmset", "-g", "therm"])
        if code == 0:
            thermal = "throttled" if re.search(r"CPU_Speed_Limit\s*=\s*(?!100\b)\d+", output) else "nominal"
        battery: int | None = None
        code, output = self._output(["pmset", "-g", "batt"])
        found = re.search(r"(\d+)%", output) if code == 0 else None
        if found:
            battery = int(found.group(1))
        free = int(shutil.disk_usage(Path.home()).free / (1024 * 1024))
        return DeviceHealth(True, locked, thermal, battery, free, tuple(notes))

    # -- build ---------------------------------------------------------------------------------------------------------------

    def toolchain(self) -> dict[str, str]:
        """What changes the build besides the project's profile (in the build cache key already): the `GDV_MACOS_BUILD_METHOD` and
        `GDV_MACOS_SIGN_SCRIPT` overrides, and the content of an absolute sign script named by the latter."""
        found: dict[str, str] = {}
        method = self._environ.get(BUILD_METHOD_ENV)
        if method:
            found["buildMethod"] = method
        script = self._environ.get(SIGN_SCRIPT_ENV)
        if script:
            found["signScript"] = script
            path = Path(script)
            if path.is_absolute() and path.is_file():
                try:
                    found["signScriptSha256"] = core.sha256_file(path)
                except OSError:
                    pass
        return found

    def build(self, project: Path, profile: str, out: Path) -> StepOutcome:
        if not self._darwin:
            return StepOutcome(constants.BLOCKED, "a macOS player is built on a Mac")
        project = project.resolve()
        version = _project_unity_version(project)
        if version is None:
            return StepOutcome(constants.BLOCKED, f"{project / 'ProjectSettings' / 'ProjectVersion.txt'} does not name a Unity version")
        editor = next((e for e in self._editors() if e["version"] == version), None)
        if editor is None:
            return StepOutcome(constants.BLOCKED, f"Unity {version} is not installed on this Mac")
        if "macos" not in editor["modules"]:
            return StepOutcome(constants.BLOCKED, f"Unity {version} has no macOS build support module")
        method = find_build_method(project, self._environ, profile)
        if method is None:
            return StepOutcome(constants.BLOCKED, f"no macOS validation build method: set {BUILD_METHOD_ENV} or executeMethod in "
                                                  f"{PROFILES_FILE} (section {PLATFORM})")
        identity = identity_for(project)
        out.mkdir(parents=True, exist_ok=True)
        app = out / f"{identity.product}.app"
        log = out.parent / "logs" / "unity-build.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        argv = [editor["path"], "-batchmode", "-quit", "-projectPath", str(project), *config.unity_accelerator_args(), "-buildTarget", "StandaloneOSX",
                "-executeMethod", method, "-buildOutput", str(app), "-gdvCompanyName", identity.company,
                "-gdvProductName", identity.product, "-logFile", str(log)]
        settings = _snapshot_settings(project)
        try:
            section = load_profile(project, profile)
            built = self._run(argv, float(section.get("timeoutSec", BUILD_TIMEOUT_SEC)), stall_sec=float(section.get("stallSec", BUILD_STALL_SEC)), log_path=out.parent / "logs" / "unity-stdio.log",
                              progress_files=(log,), cpu_counts=True)
        except BaseException:
            _restore_settings(project, settings)
            raise
        tail = _tail(log, LOG_SCAN_BYTES)
        if not _EDITOR_BUSY.search(tail):  # another Editor owns the settings then; its saves are not ours to revert
            _restore_settings(project, settings)  # a validation build never leaves the project's settings changed
        layers = {"layer.export": built.status if built.status != constants.PASS else constants.PASS}
        if built.status != constants.PASS:
            if _LICENSE.search(tail):
                return StepOutcome(constants.BLOCKED, "Unity has no valid license on this Mac (owner action: activate the Editor)", details=layers)
            if _EDITOR_BUSY.search(tail):
                return StepOutcome(constants.BLOCKED, "another Unity instance has this project open; close it or build from another checkout",
                                   details=layers)
            return StepOutcome(built.status, f"Unity build did not pass: {built.reason or 'see ' + str(log)}", details=layers)
        problem = _bundle_problem(app)
        if problem:
            return StepOutcome(constants.FAIL, f"Unity reported success but {problem}", details={"layer.export": constants.FAIL})
        signed = self._sign(project, app, profile)
        layers["layer.sign"] = signed.status
        if not signed.ok:
            return StepOutcome(signed.status, signed.reason, details=layers)
        return StepOutcome(constants.PASS, artifacts=(app,), details={**layers, "identity": f"{identity.company}/{identity.product}"})

    def _sign(self, project: Path, app: Path, profile: str = "Validation") -> StepOutcome:
        script = find_sign_script(project, self._environ, profile)
        if script is not None:
            signing = self._run([sys.executable, str(script), str(app)], TOOL_TIMEOUT_SEC * 3, stall_sec=TOOL_TIMEOUT_SEC)
            if signing.status != constants.PASS:
                if _SIGNING_UI.search(signing.output):
                    return StepOutcome(constants.NEEDS_PERMISSION, "signing needs the keychain: run the job in the GUI session "
                                                                   "(--domain gui) or unlock the login keychain (owner action)")
                return StepOutcome(signing.status if signing.status != constants.PASS else constants.FAIL,
                                   f"signing failed: {signing.output.strip()[-300:] or signing.reason}")
        verified = self._run(["codesign", "--verify", "--deep", "--strict", str(app)], TOOL_TIMEOUT_SEC, stall_sec=TOOL_TIMEOUT_SEC)
        if verified.status != constants.PASS:
            return StepOutcome(constants.FAIL, f"codesign verification failed: {verified.output.strip()[-300:] or verified.reason}")
        return StepOutcome(constants.PASS)

    # -- install -------------------------------------------------------------------------------------------------------------

    def install(self, device: DeviceInfo, artifact: Path) -> StepOutcome:
        """Registers the bundle in place (a copy would double the disk use) and gives the application a clean data folder."""
        artifact = artifact.resolve()
        problem = _bundle_problem(artifact)
        if problem:
            return StepOutcome(constants.FAIL, f"cannot install {artifact}: {problem}")
        verified = self._run(["codesign", "--verify", "--deep", "--strict", str(artifact)], TOOL_TIMEOUT_SEC, stall_sec=TOOL_TIMEOUT_SEC)
        if verified.status != constants.PASS:
            return StepOutcome(constants.FAIL, f"install refused: the bundle signature is not valid: {verified.output.strip()[-200:]}")
        info = plistlib.loads((artifact / "Contents" / "Info.plist").read_bytes())
        bundle_id = str(info.get("CFBundleIdentifier", ""))
        if not bundle_id.endswith(VALIDATION_SUFFIX):
            return StepOutcome(constants.BLOCKED, f"{artifact} has the identifier {bundle_id!r}, so its data folder is the application's own; "
                                                  f"a validation player has an identifier ending in {VALIDATION_SUFFIX!r} "
                                                  f"(build it with the Validation profile)")
        data_root = data_root_for(bundle_id, self._home)
        stopped = self.terminate(device, artifact.stem)
        if not stopped.ok:
            return stopped
        removed = self._keep_leftover(data_root)
        if not removed.ok:
            return removed
        self._reset_identity_state(artifact.stem)
        core.atomic_write_json(self._installed_path(), {
            "app": str(artifact), "bundleId": bundle_id, "executable": info["CFBundleExecutable"], "product": artifact.stem,
            "dataRoot": str(data_root), "installedUtc": core.utc_now()})
        return StepOutcome(constants.PASS)

    def _keep_leftover(self, data_root: Path) -> StepOutcome:
        """Before an install: data still there belongs to a run that did not clean up (a passing run deletes its data), so it is kept
        as diagnostics, moved aside with a manifest, instead of being cleared by another run."""
        support = (self._home or Path.home()) / _APP_DATA
        if data_root.parent != support or not data_root.name.endswith(VALIDATION_SUFFIX):
            return StepOutcome(constants.BLOCKED, f"refusing to touch {data_root}: it is not a validation data folder")
        try:
            if retention.has_content(data_root):
                retention.retain_folder(data_root, "INTERRUPTED", "an earlier run's data was still here when the next install started")
            elif data_root.exists():
                shutil.rmtree(data_root)
        except OSError as error:
            return StepOutcome(constants.FAIL, f"cannot keep the earlier validation data folder {data_root}: {error}")
        return StepOutcome(constants.PASS)

    def _reset_data(self, data_root: Path) -> StepOutcome:
        """Removes a validation data folder; only a folder named like a validation identifier below Application Support."""
        support = (self._home or Path.home()) / _APP_DATA
        if data_root.parent != support or not data_root.name.endswith(VALIDATION_SUFFIX):
            return StepOutcome(constants.BLOCKED, f"refusing to touch {data_root}: it is not a validation data folder")
        try:
            if data_root.exists():
                shutil.rmtree(data_root)
        except OSError as error:
            return StepOutcome(constants.FAIL, f"cannot clear the validation data folder {data_root}: {error}")
        return StepOutcome(constants.PASS)

    # -- files ---------------------------------------------------------------------------------------------------------------

    def push(self, device: DeviceInfo, local: Path, remote: str) -> StepOutcome:
        root = self._data_root()
        if root is None:
            return StepOutcome(constants.BLOCKED, "nothing is installed; install a build before pushing files")
        target = _relative_inside(root, remote)
        if target is None:
            return StepOutcome(constants.FAIL, f"{remote!r} is not a path inside the application's data folder")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local, target)
        except OSError as error:
            return StepOutcome(constants.FAIL, f"cannot copy {local} to {target}: {error}")
        return StepOutcome(constants.PASS)

    def pull(self, device: DeviceInfo, remote: str, local: Path) -> StepOutcome:
        root = self._data_root()
        if root is None:
            return StepOutcome(constants.BLOCKED, "nothing is installed; there is no data folder to read")
        source = _relative_inside(root, remote)
        if source is None:
            return StepOutcome(constants.FAIL, f"{remote!r} is not a path inside the application's data folder")
        if not source.exists():
            local.mkdir(parents=True, exist_ok=True)  # the application has not written there yet: empty, not an error
            return StepOutcome(constants.PASS)
        try:
            if source.is_dir():
                _copy_tree(source, local)
            else:
                local.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, local)
        except OSError as error:
            return StepOutcome(constants.FAIL, f"cannot copy {source}: {error}")
        return StepOutcome(constants.PASS)

    # -- launch and terminate ------------------------------------------------------------------------------------------------

    def _resolve_app(self, app: str) -> tuple[Path, str, Path | None] | StepOutcome:
        """Only the bundle `install` registered can be launched, by its path, bundle identifier, product or file name: a path to any
        other application would start it with that application's own data folder."""
        record = self._installed()
        if record and (app in ("", record["bundleId"], record["product"], Path(record["app"]).stem)
                       or (app.endswith(".app") and Path(app).resolve() == Path(record["app"]).resolve())):
            return Path(record["app"]), record["executable"], Path(record["dataRoot"])
        return StepOutcome(constants.BLOCKED, f"application {app!r} is not the installed validation player; install it first")

    def launch(self, device: DeviceInfo, app: str, args: Sequence[str], log: Path) -> StepOutcome:
        resolved = self._resolve_app(app)
        if isinstance(resolved, StepOutcome):
            return resolved
        bundle, executable, data_root = resolved
        binary = bundle / "Contents" / "MacOS" / executable
        if not binary.is_file() or not os.access(binary, os.X_OK):
            return StepOutcome(constants.FAIL, f"{binary} is missing or not executable")
        quarantine = self._run(["xattr", "-p", "com.apple.quarantine", str(bundle)], 20.0, stall_sec=20.0)
        if quarantine.returncode is None:
            return StepOutcome(constants.BLOCKED, "the quarantine check did not finish; the bundle is not launched unchecked")
        if quarantine.returncode == 0:
            return StepOutcome(constants.NEEDS_PERMISSION, f"{bundle.name} is quarantined; the owner approves it in Finder or System Settings "
                                                           f"(valtrion never removes the quarantine attribute)")
        stopped = self._stop_recorded()
        if not stopped.ok:
            return stopped
        argv = [str(binary), *args]
        if data_root is not None and "-gdv-plan" not in args and (data_root / "gdv" / "inbox" / "plan.json").is_file():
            argv += ["-gdv-plan", str(data_root / "gdv" / "inbox" / "plan.json")]
        if "-logFile" not in args:
            argv += ["-logFile", str(log)]
        log.parent.mkdir(parents=True, exist_ok=True)
        stdio = log.with_name(log.stem + ".stdio.log")
        try:
            with open(stdio, "ab") as sink:
                process, tree = self._spawn(argv, str(bundle.parent), None, sink, KILL_GRACE_SEC)
        except OSError as error:
            return StepOutcome(constants.BLOCKED, f"cannot start {binary}: {error}")
        self._process, self._tree = process, tree
        core.atomic_write_json(self._running_path(), {
            "pid": process.pid, "pgid": process.pid, "startToken": core.process_start_token(process.pid), "binary": str(binary),
            "startedUtc": core.utc_now()})
        deadline = time.monotonic() + self._settle_sec
        while time.monotonic() < deadline and process.poll() is None:
            self._sleep(0.1)
        code = process.poll()
        if code is None:
            return StepOutcome(constants.PASS)
        text = _tail(log) + "\n" + _tail(stdio)
        if self._tree is not None:
            self._tree.kill()  # a helper the exited player left in its group goes with it
        self._forget()
        problem = permission_problem(text) if code != 0 else None
        if problem:
            return StepOutcome(constants.NEEDS_PERMISSION, f"the application stopped on a macOS permission ({problem}); the owner grants "
                                                           f"it in System Settings > Privacy & Security (valtrion never grants it)")
        if code == 0:
            return StepOutcome(constants.PASS)
        crash = " (a signal or crash; see ~/Library/Logs/DiagnosticReports, a missing privacy usage description is reported there)" \
            if code < 0 or code >= 128 else ""
        return StepOutcome(constants.FAIL, f"the application exited with code {code} within {self._settle_sec:g} s of launch{crash}")

    def _forget(self) -> None:
        if self._tree is not None:
            self._tree.close()
        self._process = self._tree = None
        try:
            self._running_path().unlink()
        except OSError:
            pass

    def _stop_recorded(self) -> StepOutcome:
        """Stop the player this adapter started, in this process or an earlier one, never a process it cannot identify as its own."""
        if self._tree is not None:
            self._tree.kill()
            if self._process is not None:
                self._process.poll()  # reap a player that has already exited, so it is not mistaken for a running one
            if self._tree.alive():
                return StepOutcome(constants.FAIL, "the validation player did not stop after SIGTERM and SIGKILL")
            self._forget()
            return StepOutcome(constants.PASS)
        record = self._read_state(self._running_path())
        if not record:
            return StepOutcome(constants.PASS)
        pid, token = int(record.get("pid", 0)), record.get("startToken")
        group = pid  # the player leads its own session, so its group id is its pid; a recorded group id is never trusted

        def running() -> bool:
            return _group_members(group) is not False  # unknown (ps failed) counts as running: never assume a player stopped

        alive = pid > 0 and core.process_alive(pid)
        if alive:
            current = core.process_start_token(pid)
            if not token or current is None:
                return StepOutcome(constants.FAIL, f"a process (pid {pid}) may be the validation player but its identity cannot be checked")
            ours = current == token  # another start time: the pid now belongs to an unrelated process
        else:
            # The leader is gone. A group id is not reused while any member exists, so surviving members belong to the player.
            ours = _group_members(group) is not False
        if ours and running():
            for sig, wait in ((signal.SIGTERM, KILL_GRACE_SEC), (signal.SIGKILL, 3.0)):
                try:
                    os.killpg(group, sig)
                except ProcessLookupError:
                    break
                except PermissionError:
                    return StepOutcome(constants.FAIL, f"the validation player (pid {pid}) cannot be signalled")
                end = time.monotonic() + wait
                while time.monotonic() < end and running():
                    self._sleep(0.05)
                if not running():
                    break
            if running():
                return StepOutcome(constants.FAIL, f"the validation player (pid {pid}) did not stop")
        self._forget()
        return StepOutcome(constants.PASS)

    def terminate(self, device: DeviceInfo, app: str) -> StepOutcome:
        return self._stop_recorded()

    # -- screenshot and cleanup ----------------------------------------------------------------------------------------------

    def screenshot(self, device: DeviceInfo, out: Path) -> StepOutcome:
        """The harness's own capture (`gdv/out/<run>/screenshots/*.png`), which needs no screen-recording permission. Without one the
        screen is captured with `screencapture`; if macOS refuses, that is a missing permission, not a failure of the run."""
        root = self._data_root()
        if root is not None:
            shots = sorted((p for p in (root / "gdv" / "out").rglob("*.png") if p.is_file()), key=lambda p: p.stat().st_mtime) \
                if (root / "gdv" / "out").is_dir() else []
            if shots:
                out.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(shots[-1], out)
                except OSError as error:
                    return StepOutcome(constants.FAIL, f"cannot copy {shots[-1]}: {error}")
                return StepOutcome(constants.PASS)
        out.parent.mkdir(parents=True, exist_ok=True)
        captured = self._run(["screencapture", "-x", str(out)], 30.0, stall_sec=30.0)
        if captured.status == constants.PASS and out.is_file() and out.stat().st_size > 0:
            return StepOutcome(constants.PASS)
        detail = f"{captured.output.strip()[-160:] or captured.reason}"
        if "could not create image" in captured.output:
            return StepOutcome(constants.NEEDS_PERMISSION, "the harness left no screenshot and macOS did not allow a screen capture "
                                                          f"(Screen Recording permission is an owner action): {detail}")
        return StepOutcome(constants.BLOCKED, f"the harness left no screenshot and the screen could not be captured: {detail}")

    def cleanup_run_data(self, device: DeviceInfo) -> StepOutcome:
        """Removes the isolated data folder after the results were collected. Called by the pipeline at the end of a run."""
        record = self._installed()
        if not record:
            return StepOutcome(constants.PASS)
        stopped = self._stop_recorded()  # a player this adapter started is stopped first; one that cannot be stopped keeps its data
        if not stopped.ok:
            return StepOutcome(constants.BLOCKED, f"the data folder is kept because the player is still running: {stopped.reason}")
        removed = self._reset_data(Path(record["dataRoot"]))
        if removed.ok:
            self._reset_identity_state(record.get("product", ""))
        return removed

    def retain_run_data(self, device: DeviceInfo, status: str, reason: str | None) -> StepOutcome:
        """Keeps the isolated data folder of a failed or interrupted run as diagnostics: the player is stopped, a manifest is written
        into the folder and the folder is moved aside to `<identifier>.gdv.retained-<utc>`, which no later install or cleanup clears.
        Removing it is the job of the task that started the run, after review."""
        record = self._installed()
        if not record:
            return StepOutcome(constants.PASS)
        stopped = self._stop_recorded()
        if not stopped.ok:
            return StepOutcome(constants.BLOCKED, f"the data folder is left in place because the player is still running: {stopped.reason}")
        data_root = Path(record["dataRoot"])
        support = (self._home or Path.home()) / _APP_DATA
        if data_root.parent != support or not data_root.name.endswith(VALIDATION_SUFFIX):
            return StepOutcome(constants.BLOCKED, f"refusing to touch {data_root}: it is not a validation data folder")
        if not retention.has_content(data_root):
            return StepOutcome(constants.PASS)  # the run left nothing worth keeping
        try:
            target = retention.retain_folder(data_root, status, reason)
        except OSError as error:
            return StepOutcome(constants.FAIL, f"cannot keep the validation data folder {data_root}: {error}")
        return StepOutcome(constants.PASS, details={"retainedAt": str(target)})

    def _reset_identity_state(self, product: str) -> None:
        """Preferences and caches of the validation identity (PlayerPrefs of an earlier run must not reach the next)."""
        if not product.endswith("-gdv"):
            return
        home = self._home or Path.home()
        for path in (home / "Library" / "Preferences" / f"unity.{COMPANY}.{product}.plist", home / "Library" / "Caches" / COMPANY / product):
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
            except OSError:
                pass


def _project_unity_version(project: Path) -> str | None:
    try:
        text = (project / "ProjectSettings" / "ProjectVersion.txt").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"m_EditorVersion:\s*(\S+)", text)
    return match.group(1) if match else None


def _bundle_problem(app: Path) -> str | None:
    if app.suffix != ".app" or not app.is_dir():
        return f"{app} is not an .app bundle"
    try:
        info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    except (OSError, ValueError):
        return f"{app} has no readable Contents/Info.plist"
    executable = info.get("CFBundleExecutable")
    if not executable or not (app / "Contents" / "MacOS" / executable).is_file():
        return f"{app} has no executable in Contents/MacOS"
    return None


def _group_members(group: int) -> bool | None:
    """Whether a process group still has a non-zombie member; None when `ps` could not tell."""
    try:
        output = subprocess.run(["ps", "-axo", "pgid=,stat="], capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == str(group) and not parts[1].startswith("Z"):
            return True
    return False


def _snapshot_settings(project: Path) -> dict[Path, bytes]:
    """The project settings files before a build. Unity saves `ProjectSettings` when the Editor quits, which would leave the
    working copy changed (and a result marked dirty) after the build method restored its in-memory values."""
    folder = project / "ProjectSettings"
    snapshot: dict[Path, bytes] = {}
    try:
        for path in folder.glob("*"):
            if path.is_file() and path.stat().st_size <= 8 * 1024 * 1024:
                snapshot[path] = path.read_bytes()
    except OSError:
        return {}
    return snapshot


def _restore_settings(project: Path, snapshot: Mapping[Path, bytes]) -> None:
    for path, data in snapshot.items():
        try:
            if not path.is_file() or path.read_bytes() != data:
                path.write_bytes(data)
        except OSError:
            pass


def _copy_tree(source: Path, destination: Path) -> None:
    """Copies a folder the application may be writing: a file that vanishes mid-copy is skipped (the next poll sees it)."""
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        try:
            if path.is_dir():
                (destination / relative).mkdir(parents=True, exist_ok=True)
            else:
                (destination / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination / relative)
        except FileNotFoundError:
            continue


register(PLATFORM, MacOSAdapter)
