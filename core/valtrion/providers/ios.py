"""The `ios` platform adapter: Unity batchmode export, `xcodebuild` in the GUI session, `devicectl` install, file transfer and launch.

The adapter runs ON the Mac host. A Windows caller reaches it through `HostResolver` and the SSH transport (the whole pipeline is a
detached job on the Mac); a Mac caller runs it locally with no SSH. Signing needs the login keychain, so a build only runs inside
a GUI-session launchd job (`launchctl managername` is `Aqua`); plain SSH `codesign` fails with `errSecInternalComponent`.

Every external tool call is bounded by `supervise.run_bounded` (hard timeout, stall window, process tree kill), including each
`devicectl copy`. The device is always taken from `devicectl list devices`: a UDID is never trusted from a script or a stale
registry, the registry only adds the owner's class, capabilities and alias to a device that discovery just found.

Build settings come from the project: `validation/device/profiles.json`,

    {"profiles": {"Internal": {"ios": {"executeMethod": "My.Build.Editor.IosBuild.Internal",
                                         "buildProfile": "Assets/Build/Profiles/Internal.asset",
                                         "configuration": "ReleaseForRunning"}}}}

`executeMethod` must export the Xcode project to the folder passed as `-gdv-export-path <folder>` (also given as `-buildOutput
<folder>`, the switch the other Unity build methods read); a method that ignores both and exports to a fixed project folder
names it as `exportPath`. Files the application creates on the device are removed
through the harness ledger or by reinstalling: `devicectl` cannot delete.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import unquote

from .. import config, constants, core, incremental, supervise
from ..adapter import Capabilities, DeviceHealth, DeviceInfo, PlatformAdapter, StepOutcome
from . import register

PLATFORM = "ios"
PROFILES_FILE = Path("validation") / "device" / "profiles.json"
UNITY_EDITORS_ENV = "GDV_UNITY_EDITORS"
DEFAULT_UNITY_EDITORS = "/Applications/Unity/Hub/Editor"
TOOL_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
#: Result detail naming what `xcodebuild` was given to build: `workspace` (CocoaPods export) or `project`.
INCREMENTAL_DETAIL = "incremental.note"
XCODE_CONTAINER_DETAIL = "xcodebuild.container"
APP_DATA_ROOT = "Documents"  # `Application.persistentDataPath` of an iOS player, relative to the app data container

#: Bounds of one devicectl call. A copy that stalls longer than the stall window is killed, not waited for.
DEVICECTL_TIMEOUT_SEC = 120.0
LIST_TIMEOUT_SEC = 60.0
WAKE_TIMEOUT_SEC = 30.0  # the lock-state probe that brings the idle tunnel of a wired device up
INSTALL_TIMEOUT_SEC = 600.0
LAUNCH_WAIT_SEC = 90.0
DEFAULT_EXPORT_TIMEOUT_SEC = 3600.0
DEFAULT_BUILD_TIMEOUT_SEC = 3600.0

_UNITY_VERSION = re.compile(r"m_EditorVersion:\s*(\S+)")
#: Only a missing file inside the app container; "device not found" or "app not found" are real failures and must stay failures.
_NOT_FOUND = re.compile(r"Failed to retrieve the file node|No such file or directory", re.IGNORECASE)
#: Top-level project folders an `exportPath` may never be in (or be), because the export is removed after the run.
PROTECTED_PROJECT_FOLDERS = ("Assets", "Packages", "ProjectSettings", "Library", "UserSettings", "Logs", "Temp", "validation", "tools")
#: The whole build (lfs, Unity export, xcodebuild) ends inside this, which is below the pipeline's default build stage timeout.
MAX_BUILD_SEC = 14000.0
_SIGNING = re.compile(
    r"errSecInternalComponent|User interaction is not allowed|passphrase you entered is not correct|"
    r"No signing certificate|No profiles for|requires a provisioning profile|code signing is required", re.IGNORECASE)
_LOCKED = re.compile(r"device is locked|device was locked|was not, or could not be, unlocked|unlock the device|passcode required",
                     re.IGNORECASE)
_PROFILE_KEYS = {
    "executeMethod", "buildProfile", "configuration", "scheme", "exportPath", "unityArgs", "exportTimeoutSec", "buildTimeoutSec",
    "stallSec", "gitLfsPull", "destination", "xcodeProject", "unityPath", "xcodebuildSettings"}
_BUILD_SETTING = re.compile(r"[A-Z][A-Z0-9_]*")

#: Product types the owner's classes are named after; anything else gets a class derived from its product type.
_CLASS_BY_PRODUCT = {"iPad13,18": "ipad-a14", "iPad13,19": "ipad-a14"}

Runner = Callable[..., supervise.BoundedResult]


class BuildSettingsError(Exception):
    """The project's profiles.json does not describe an iOS build for the requested profile."""


@dataclass(frozen=True)
class BuildSettings:
    execute_method: str
    build_profile: str | None = None
    configuration: str = "Release"
    scheme: str = "Unity-iPhone"
    export_path: str | None = None
    xcode_project: str | None = None
    unity_args: tuple[str, ...] = ()
    export_timeout_sec: float = DEFAULT_EXPORT_TIMEOUT_SEC
    build_timeout_sec: float = DEFAULT_BUILD_TIMEOUT_SEC
    stall_sec: float = supervise.DEFAULT_STALL_SEC
    git_lfs_pull: bool = False
    destination: str = "device"  # "device": the discovered device; "generic": generic/platform=iOS
    unity_path: str | None = None  # a Unity binary other than the Hub editor of ProjectVersion.txt (a pinned or test editor)
    xcodebuild_settings: tuple[str, ...] = ()  # `KEY=VALUE` build settings, for example GCC_ENABLE_OBJC_EXCEPTIONS=YES


def load_build_settings(project: Path, profile: str) -> BuildSettings:
    path = project / PROFILES_FILE
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as error:
        raise BuildSettingsError(f"cannot read {path}: {error}") from error
    except ValueError as error:
        raise BuildSettingsError(f"{path} is not valid JSON: {error}") from error
    entry = (document.get("profiles", {}).get(profile, {}) if isinstance(document, dict) else {}).get(PLATFORM)
    if not isinstance(entry, dict):
        raise BuildSettingsError(f"{path} has no profiles.{profile}.{PLATFORM} entry")
    unknown = sorted(set(entry) - _PROFILE_KEYS)
    if unknown:
        raise BuildSettingsError(f"{path} profiles.{profile}.{PLATFORM} has unknown keys: {', '.join(unknown)}")
    method = entry.get("executeMethod")
    if not isinstance(method, str) or not method:
        raise BuildSettingsError(f"{path} profiles.{profile}.{PLATFORM}.executeMethod is required")
    if entry.get("destination", "device") not in ("device", "generic"):
        raise BuildSettingsError(f"{path} destination must be 'device' or 'generic'")
    extra = entry.get("xcodebuildSettings", {})
    if not isinstance(extra, dict) or not all(_BUILD_SETTING.fullmatch(str(k)) and isinstance(v, str) for k, v in extra.items()):
        raise BuildSettingsError(f"{path} xcodebuildSettings must map UPPER_CASE setting names to string values")
    try:
        return BuildSettings(
            method, entry.get("buildProfile"), entry.get("configuration", "Release"), entry.get("scheme", "Unity-iPhone"),
            entry.get("exportPath"), entry.get("xcodeProject"), tuple(str(a) for a in entry.get("unityArgs", ())),
            float(entry.get("exportTimeoutSec", DEFAULT_EXPORT_TIMEOUT_SEC)), float(entry.get("buildTimeoutSec", DEFAULT_BUILD_TIMEOUT_SEC)),
            float(entry.get("stallSec", supervise.DEFAULT_STALL_SEC)), bool(entry.get("gitLfsPull", False)),
            entry.get("destination", "device"), entry.get("unityPath"), tuple(f"{k}={v}" for k, v in sorted(extra.items())))
    except (TypeError, ValueError) as error:
        raise BuildSettingsError(f"{path} profiles.{profile}.{PLATFORM} has a malformed value: {error}") from error


def _get(document: Mapping[str, Any], *path: str) -> Any:
    current: Any = document
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def parse_devices(document: Mapping[str, Any]) -> list[dict]:
    """Devices of `devicectl list devices --json-output`: udid, name, product type, OS, state, reality. Both the current
    `properties` dictionary and the deprecated `*Properties` fields are read, so a newer or older Xcode keeps working."""
    found = []
    for item in _get(document, "result", "devices") or []:
        udid = _get(item, "properties", "hardware", "udid") or _get(item, "hardwareProperties", "udid")
        if not udid:
            continue
        found.append({
            "udid": str(udid),
            "identifier": str(item.get("identifier", "")),
            "name": _get(item, "deviceProperties", "name") or _get(item, "properties", "device", "name") or "",
            "product": _get(item, "properties", "hardware", "productType") or _get(item, "hardwareProperties", "productType") or "",
            "marketing": _get(item, "properties", "hardware", "marketingName") or _get(item, "hardwareProperties", "marketingName") or "",
            "reality": _get(item, "properties", "hardware", "reality") or _get(item, "hardwareProperties", "reality") or "",
            "platform": _get(item, "properties", "hardware", "platform") or _get(item, "hardwareProperties", "platform") or "",
            "os": _get(item, "deviceProperties", "osVersionNumber") or _get(item, "properties", "software", "osVersionNumber") or "",
            "state": _get(item, "properties", "connection", "state") or _get(item, "connectionProperties", "tunnelState") or "",
            "pairing": _get(item, "properties", "connection", "pairingState") or _get(item, "connectionProperties", "pairingState") or "",
            "transport": _get(item, "properties", "connection", "transportType") or _get(item, "connectionProperties", "transportType") or ""})
    return found


def _wired_and_paired(item: Mapping[str, str]) -> bool:
    """A physical device on the USB cable that trusts this Mac. CoreDevice drops the tunnel of an idle device (`available (paired)`,
    `tunnelState: disconnected`) and brings it up again for the next `devicectl device` command, so such a device is attached."""
    return item["transport"] == "wired" and item["pairing"] == "paired" and item["reality"] in ("", "physical")


def attachable(item: Mapping[str, str]) -> bool:
    """Whether a listed device can be used now: connected and paired, or wired and paired with an idle tunnel. A wireless device
    whose tunnel is down is not: it may be anywhere on the network, or nowhere."""
    if item["platform"] and item["platform"] != "iOS":
        return False
    if item["pairing"] not in ("", "paired"):
        return False
    return item["state"] == "connected" or _wired_and_paired(item)


def derive_class(product: str, marketing: str) -> str:
    if product in _CLASS_BY_PRODUCT:
        return _CLASS_BY_PRODUCT[product]
    family = re.sub(r"[^a-z0-9]+", "-", (product or marketing or "ios").lower()).strip("-")
    return family or "ios"


def _device_path(value: str) -> str:
    """A path as `devicectl` prints it (`file:///private/var/...`, percent-encoded or plain, `/var` or `/private/var`), made
    comparable."""
    path = unquote(value[len("file://"):] if value.startswith("file://") else value)
    return path[len("/private"):] if path.startswith("/private/") else path


def udid_of(device: DeviceInfo) -> str:
    return device.id.split(":", 1)[1] if ":" in device.id else device.id


def unity_version_of(project: Path) -> str | None:
    try:
        match = _UNITY_VERSION.search((project / "ProjectSettings" / "ProjectVersion.txt").read_text(encoding="utf-8"))
    except OSError:
        return None
    return match.group(1) if match else None


def _tool_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = TOOL_PATH + os.pathsep + env.get("PATH", "")
    env.update(extra or {})
    return env


def classify_xcodebuild_failure(output: str) -> tuple[str, str]:
    """(layer, status) of a failed `xcodebuild`: a signing problem needs the owner, anything else is a build failure."""
    if _SIGNING.search(output):
        return "sign", constants.NEEDS_PERMISSION
    return "nativeBuild", constants.FAIL


def _not_in_gui_session(manager: str) -> str:
    """Why the build is not in the GUI session, from the launchd domain the job runner recorded (`core.JOB_DOMAIN_ENV`)."""
    domain = os.environ.get(core.JOB_DOMAIN_ENV)
    if domain is not None and domain.startswith("gui/"):
        return (f"iOS signing needs the login keychain, but this job already runs in the {domain} launchd domain and launchd reports the "
                f"session as {manager or 'unknown'}, not Aqua: no user is logged in to the Mac desktop, or its login keychain is locked; "
                "log in at the Mac and unlock the login keychain (owner action), restarting the job with the same --domain gui will not help")
    where = (f"the {domain} launchd domain" if domain else
             "the SSH or terminal context (--foreground or no launchd domain)")
    return (f"iOS signing needs the GUI session, but this job ran in {where}; start it without --foreground, with --domain gui "
            "(`valtrion run --domain gui`); plain SSH codesign fails with errSecInternalComponent")


class IosAdapter(PlatformAdapter):
    platform = PLATFORM
    #: Signing needs the login keychain of the GUI session (`valtrion run` starts the job in the `gui` launchd domain).
    launchd_domain = "gui"
    #: The build is signed for the device the pipeline selected, so a cached build is only reused for that device.
    signs_for_device = True

    def __init__(self, run: Runner | None = None, spawn: Callable[..., Any] | None = None, sleep: Callable[[float], None] = time.sleep,
                 os_name: str | None = None, home: Path | None = None) -> None:
        self._run = run or supervise.run_bounded
        self._spawn = spawn or supervise.spawn
        self._sleep = sleep
        self._os_name = os_name or core.local_os()
        self._home = home
        self._app: str | None = None
        self._device: DeviceInfo | None = None
        self._register_temp: Callable[[Path], None] | None = None
        self._incremental: tuple[incremental.Plan, str | None] | None = None
        self._stage_budget: float | None = None
        self._consoles: dict[str, tuple[Any, supervise.ProcessTree, Any]] = {}

    # -- application binding -------------------------------------------------------------------------------------------------

    def bind_application(self, app: str) -> None:
        self._app = app

    def bind_device(self, device: DeviceInfo) -> None:
        self._device = device

    def prepare(self, device: DeviceInfo, app: str, data_policy: str | None) -> StepOutcome:
        self._app = app
        if data_policy == "clean-install":
            # devicectl cannot delete app data; a clean state means uninstalling and reinstalling the build, which a run stage cannot do.
            return StepOutcome(constants.BLOCKED, "dataPolicy clean-install is not supported on iOS: devicectl cannot delete app data; "
                                                  "reinstall the build in a pipeline (build and install stages) before the run")
        return StepOutcome(constants.PASS)

    def bind_temporary_registrar(self, register: Callable[[Path], None]) -> None:
        self._register_temp = register

    def bind_build_budget(self, seconds: float) -> None:
        self._stage_budget = seconds

    def bind_incremental(self, plan: incremental.Plan | None, lease_resource: str | None = None) -> None:
        """Build in the managed checkout's persistent folder (see `incremental.py`) instead of a fresh export in the job work folder."""
        self._incremental = (plan, lease_resource) if plan is not None else None

    def _require_app(self) -> str | None:
        return self._app

    def toolchain(self) -> dict[str, str]:
        """The Xcode version, part of the build cache key; raises when it cannot be read (the build is then not cached)."""
        xcodebuild = shutil.which("xcodebuild", path=TOOL_PATH)
        if xcodebuild is None:
            raise RuntimeError("xcodebuild is not installed")
        version = self._run([xcodebuild, "-version"], 30, env=_tool_env())
        if not version.ok or not version.output.strip():
            raise RuntimeError(f"xcodebuild -version failed: {(version.reason or version.output).strip()[:160]}")
        return {"xcode": " ".join(version.output.split())}

    # -- probe and discovery -------------------------------------------------------------------------------------------------

    def probe(self) -> Capabilities:
        if self._os_name != "macos":
            return Capabilities(PLATFORM, (), {"ios": f"iOS builds and devices need a macOS host with Xcode; this host is {self._os_name}"})
        available: list[str] = []
        blocked: dict[str, str] = {}
        xcrun = shutil.which("xcrun", path=TOOL_PATH)
        if xcrun is None:
            blocked["xcode"] = "xcrun is not installed (Xcode command line tools)"
            return Capabilities(PLATFORM, (), blocked)
        version = self._run([xcrun, "devicectl", "--version"], 30, env=_tool_env())
        if version.ok:
            available.append("devicectl")
        else:
            blocked["devicectl"] = f"xcrun devicectl failed: {(version.reason or version.output).strip()[:160]}"
        manager = self._run(["launchctl", "managername"], 15)
        if manager.ok and manager.output.strip() == "Aqua":
            available.append("gui-session")
        else:
            blocked["gui-session"] = ("this process is not in the logged-in GUI session; signing needs a job in the gui launchd domain "
                                      f"({manager.output.strip() or manager.reason or 'unknown session'})")
        if not Path(os.environ.get(UNITY_EDITORS_ENV, DEFAULT_UNITY_EDITORS)).is_dir():
            blocked["unity"] = f"no Unity Hub editors folder at {os.environ.get(UNITY_EDITORS_ENV, DEFAULT_UNITY_EDITORS)}"
        else:
            available.append("unity")
        return Capabilities(PLATFORM, tuple(available), blocked)

    def _list_devices(self) -> list[dict]:
        listing = self._devicectl_json(["list", "devices"], LIST_TIMEOUT_SEC)
        if listing is None:
            raise RuntimeError("`devicectl list devices` failed or returned no JSON")
        return parse_devices(listing)

    def discover(self) -> Sequence[DeviceInfo]:
        items = self._list_devices()
        idle = [item for item in items if attachable(item) and item["state"] != "connected"]
        if idle:
            # Wake the idle tunnels before anyone uses the devices: any `devicectl device` command brings a wired device's tunnel up,
            # and the lock state is the cheapest one. Listed once more so model, OS and state are those of the woken device.
            for item in idle:
                self._devicectl(["device", "info", "lockState", "--device", item["udid"]], WAKE_TIMEOUT_SEC)
            items = self._list_devices()
        try:
            registry = {d["id"]: d for d in config.load_registry(self._home).get("devices", [])}
        except Exception:  # noqa: BLE001 - a broken registry must not hide a connected device
            registry = {}
        devices = []
        for item in items:
            if not attachable(item):
                continue
            device_id = f"{PLATFORM}:{item['udid']}"
            known = registry.get(device_id, {})
            physical = item["reality"] in ("", "physical")
            capabilities = tuple(sorted(set(known.get("capabilities", ())) | {"touch"}))
            devices.append(DeviceInfo(
                device_id, PLATFORM, known.get("class") or derive_class(item["product"], item["marketing"]),
                constants.EVIDENCE_PHYSICAL if physical else constants.EVIDENCE_EMULATOR,
                f"{item['marketing']} ({item['product']})".strip() if item["marketing"] else item["product"],
                f"iPadOS {item['os']}" if item["product"].startswith("iPad") and item["os"] else
                f"iOS {item['os']}" if item["os"] else "", capabilities, True))
        return devices

    # -- build ---------------------------------------------------------------------------------------------------------------

    def build(self, project: Path, profile: str, out: Path) -> StepOutcome:
        layers: dict[str, str] = {}
        temporary: dict[str, str] = {}
        notes: dict[str, str] = {}
        logs = out.parent / "build-logs"  # beside `out`: the pipeline removes `out` after the result, diagnostics stay

        def done(status: str, reason: str | None, artifacts: Sequence[Path] = ()) -> StepOutcome:
            # `temp.*` details name extra folders this build created; the pipeline removes them with `out` once results are saved.
            return StepOutcome(status, reason, tuple(artifacts), {**{f"layer.{k}": v for k, v in layers.items()}, **temporary, **notes})

        if self._os_name != "macos":
            return done(constants.BLOCKED, f"an iOS build needs a macOS host; this host is {self._os_name}")
        try:
            settings = load_build_settings(project, profile)
        except BuildSettingsError as error:
            return done(constants.BLOCKED, str(error))
        manager = self._run(["launchctl", "managername"], 15)
        if manager.ok and manager.output.strip() != "Aqua":
            return done(constants.BLOCKED, _not_in_gui_session(manager.output.strip()))
        unity = self._unity_binary(project, settings.unity_path)
        if unity is None:
            return done(constants.BLOCKED, f"Unity {unity_version_of(project) or '(version unreadable)'} is not installed in "
                                           f"{os.environ.get(UNITY_EDITORS_ENV, DEFAULT_UNITY_EDITORS)}")
        out.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        persistent = self._incremental[0] if self._incremental is not None else None
        prepared = incremental.prepare(persistent, self._incremental[1]) if persistent is not None else None
        if prepared is not None:
            export = prepared.export  # outside the project and the job work folder; the next build there reuses it
            notes[INCREMENTAL_DETAIL] = prepared.note
        elif settings.export_path:
            export = (project / settings.export_path).resolve()
            relative = export.relative_to(project.resolve()).parts if project.resolve() in export.parents else ()
            # APFS is case-insensitive: "assets/Gen" is the Assets folder.
            if not relative or relative[0].lower() in {p.lower() for p in PROTECTED_PROJECT_FOLDERS} or relative[0].startswith("."):
                return done(constants.BLOCKED, f"exportPath {settings.export_path} must be a new folder inside the project, "
                                               f"outside {', '.join(PROTECTED_PROJECT_FOLDERS)}")
            if export.exists():
                return done(constants.BLOCKED, f"exportPath {settings.export_path} already exists; the build removes its export "
                                               "afterwards, so it must be a folder this run creates (delete it or choose another)")
            # The export's parent folders are created here when they are missing (inside the project, never outside). The topmost
            # folder this run creates is the one listed for removal, so the whole chain goes with the export and nothing else does.
            missing: list[Path] = []
            probe = export
            while not probe.exists():
                missing.append(probe)
                probe = probe.parent
            if not probe.is_dir():
                return done(constants.BLOCKED, f"{probe} is not a folder, so exportPath {settings.export_path} cannot be created in it")
            made: list[Path] = []
            try:
                for folder in reversed(missing):  # top first, one level at a time, never with exist_ok: a folder someone else made is theirs
                    folder.mkdir()
                    made.append(folder)
            except OSError as error:
                for folder in reversed(made):
                    try:
                        folder.rmdir()
                    except OSError:
                        pass
                return done(constants.BLOCKED, f"cannot create exportPath {settings.export_path}: {error}")
            created = made[0]
            temporary["temp.export"] = str(created)  # only a folder this run created is ever listed for removal
            if self._register_temp is not None:
                self._register_temp(created)  # at once, so a timeout, cancel or crash mid-build still cleans it up
        else:
            export = out / "export"
            export.mkdir(parents=True, exist_ok=True)

        started = time.monotonic()
        budget = min(sum((900.0 if settings.git_lfs_pull else 0.0, settings.export_timeout_sec, settings.build_timeout_sec)), MAX_BUILD_SEC)
        if self._stage_budget is not None:  # the pipeline abandons a build at the stage timeout; end it a little before
            budget = min(budget, max(self._stage_budget - min(60.0, self._stage_budget / 2), 1.0))

        def remaining() -> float:
            """What is left of the whole build: every tool call is capped by it, so the build ends inside the stage timeout and
            never leaves a Unity or xcodebuild running under a pipeline that has already given up."""
            return max(budget - (time.monotonic() - started), 1.0)  # a call that starts with no time left ends at once as TIMEOUT

        if settings.git_lfs_pull:
            lfs = self._run(["git", "lfs", "pull"], min(900.0, remaining()), cwd=str(project), env=_tool_env(), log_path=logs / "lfs.log")
            if not lfs.ok:
                layers["export"] = constants.FAIL
                return done(lfs.status, f"git lfs pull failed: {lfs.reason or lfs.output.strip()[-200:]}")

        unity_log = logs / "unity-export.log"
        argv = [str(unity), "-batchmode", "-quit", "-projectPath", str(project), *config.unity_accelerator_args(self._home)]
        if settings.build_profile:
            argv += ["-activeBuildProfile", settings.build_profile]
        # `-buildOutput` is the switch other Unity build methods already read; `-gdv-export-path` is the neutral name.
        argv += ["-executeMethod", settings.execute_method, "-gdv-export-path", str(export), "-buildOutput", str(export),
                 "-logFile", str(unity_log), *settings.unity_args]
        if prepared is not None and prepared.append:
            # The export method adds BuildOptions.AcceptExternalModificationsToPlayer (Append) when it is given this switch, so Unity
            # changes the Xcode project in place and Xcode recompiles only what changed.
            argv += ["-gdv-export-append", "1"]
        # A hung export is TIMEOUT or STALL (the Unity log and the CPU of the editor both stopped), not a wait without end.
        exported = self._run(argv, min(settings.export_timeout_sec, remaining()), settings.stall_sec, str(project), _tool_env(),
                             logs / "unity-export.out", progress_files=(unity_log,), cpu_counts=True)
        layers["export"] = exported.status
        if not exported.ok:
            if exported.status in (constants.STALL, constants.TIMEOUT) and persistent is not None:
                incremental.discard(persistent.folder)  # killed half way: the export cannot be trusted
            return done(exported.status, f"Unity export {exported.status.lower()}: "
                                         f"{exported.reason or 'exit code ' + str(exported.returncode)}; see {unity_log}")
        xcodeproj = self._find_xcodeproj(export, settings.xcode_project)
        if xcodeproj is None:
            layers["export"] = constants.FAIL
            return done(constants.FAIL, f"the export produced no Xcode project in {export}")

        derived = prepared.derived if prepared is not None else out / "DerivedData"
        destination = "generic/platform=iOS"
        if settings.destination == "device":
            try:
                physical = [d for d in self.discover() if d.evidence == constants.EVIDENCE_PHYSICAL]
            except Exception as error:  # noqa: BLE001 - discovery is a tool call
                return done(constants.BLOCKED, f"no device to sign for: {error}")
            if self._device is not None:  # the device the pipeline selected and leased, not just the first attached one
                physical = [d for d in physical if d.id == self._device.id]
                if not physical:
                    return done(constants.BLOCKED, f"the selected device {self._device.id} is not attached any more")
            if not physical:
                return done(constants.BLOCKED, "no connected physical iOS device to build for (profiles destination 'device')")
            destination = f"platform=iOS,id={udid_of(sorted(physical, key=lambda d: d.id)[0])}"
        xcode_log = logs / "xcode-build.log"
        # A CocoaPods export (External Dependency Manager runs `pod install`) must be built through its workspace: with `-project`
        # the link fails with `framework 'Pods_UnityFramework' not found`.
        workspace = xcodeproj.parent / f"{xcodeproj.stem}.xcworkspace"
        container = ["-workspace", str(workspace)] if workspace.is_dir() else ["-project", str(xcodeproj)]
        notes[XCODE_CONTAINER_DETAIL] = container[0][1:]
        built = self._run(
            ["xcodebuild", *container, "-scheme", settings.scheme, "-configuration", settings.configuration,
             "-destination", destination, "-allowProvisioningUpdates", "-derivedDataPath", str(derived), "build",
             *settings.xcodebuild_settings],
            min(settings.build_timeout_sec, remaining()), settings.stall_sec, str(xcodeproj.parent), _tool_env(), xcode_log,
            progress_files=(derived,), cpu_counts=True)
        if not built.ok:
            if built.status in (constants.STALL, constants.TIMEOUT):
                if persistent is not None:
                    incremental.discard(persistent.folder)  # killed half way: neither the project nor DerivedData can be trusted
                layers["nativeBuild"] = built.status
                return done(built.status, f"xcodebuild {built.status.lower()}: {built.reason}; see {xcode_log}")
            layer, status = classify_xcodebuild_failure(built.output)
            layers["nativeBuild"] = constants.FAIL
            if _LOCKED.search(built.output):
                return done(constants.NEEDS_PERMISSION, f"the device is locked; unlock it (owner action); see {xcode_log}")
            if layer == "sign":
                layers["sign"] = constants.FAIL
                return done(constants.NEEDS_PERMISSION, "code signing needs the owner (login keychain locked or no signing identity "
                                                        f"or provisioning profile); see {xcode_log}")
            return done(constants.FAIL, f"xcodebuild exited {built.returncode}; see {xcode_log}")
        apps = sorted((derived / "Build" / "Products" / f"{settings.configuration}-iphoneos").glob("*.app")) \
            if (derived / "Build" / "Products").is_dir() else []
        if not apps:
            layers["nativeBuild"] = constants.FAIL
            return done(constants.FAIL, f"xcodebuild succeeded but produced no .app in Build/Products/{settings.configuration}-iphoneos")
        layers["nativeBuild"] = constants.PASS
        layers["sign"] = constants.PASS
        app = apps[0]
        if persistent is not None:
            # The product stays in the persistent DerivedData so the next build can reuse it; the pipeline publishes a copy.
            copy = out / app.name
            if copy.exists():
                shutil.rmtree(copy, ignore_errors=True)
            try:
                shutil.copytree(app, copy, symlinks=True)
            except OSError as error:
                return done(constants.FAIL, f"cannot copy the built app out of the persistent DerivedData: {error}")
            app = copy
            incremental.commit(persistent)
        return done(constants.PASS, None, (app,))

    def _unity_binary(self, project: Path, override: str | None = None) -> Path | None:
        if override:
            return Path(override) if Path(override).is_file() else None
        version = unity_version_of(project)
        if version is None:
            return None
        binary = Path(os.environ.get(UNITY_EDITORS_ENV, DEFAULT_UNITY_EDITORS)) / version / "Unity.app" / "Contents" / "MacOS" / "Unity"
        return binary if binary.is_file() else None

    @staticmethod
    def _find_xcodeproj(export: Path, name: str | None) -> Path | None:
        if name:
            candidate = export / name
            return candidate if candidate.is_dir() else None
        found = sorted(export.glob("*.xcodeproj"))
        return found[0] if found else None

    # -- install -------------------------------------------------------------------------------------------------------------

    def install(self, device: DeviceInfo, artifact: Path) -> StepOutcome:
        if not artifact.is_dir() or artifact.suffix != ".app":
            return StepOutcome(constants.BLOCKED, f"install refused: {artifact} is not a built .app (the build did not produce one)")
        result = self._devicectl(["device", "install", "app", "--device", udid_of(device), str(artifact)], INSTALL_TIMEOUT_SEC)
        return self._outcome(result, "devicectl install failed", {"layer.install": constants.PASS if result.ok else result.status})

    # -- files ---------------------------------------------------------------------------------------------------------------

    def _container_path(self, remote: str) -> str:
        return remote if remote.startswith(APP_DATA_ROOT + "/") or remote == APP_DATA_ROOT else f"{APP_DATA_ROOT}/{remote.lstrip('/')}"

    def push(self, device: DeviceInfo, local: Path, remote: str) -> StepOutcome:
        app = self._require_app()
        if not app:
            return StepOutcome(constants.BLOCKED, "push needs the application id (bind_application was not called)")
        if not local.exists():
            return StepOutcome(constants.FAIL, f"{local} does not exist")
        destination = self._container_path(remote)
        copy = ["device", "copy", "to", "--device", udid_of(device), "--domain-type", "appDataContainer", "--domain-identifier", app]
        result = self._devicectl([*copy, "--source", str(local), "--destination", destination], DEVICECTL_TIMEOUT_SEC)
        if result.ok or result.status != constants.FAIL or "/" not in remote.strip("/") or not _NOT_FOUND.search(result.output):
            return self._outcome(result, f"push to {destination} failed")
        # The folder is not on the device yet (first run after an install): a copied directory tree creates it.
        with tempfile.TemporaryDirectory(prefix="gdv-push-") as stage:
            parts = Path(destination).parts
            root = Path(stage) / parts[1]
            target = root.joinpath(*parts[2:])
            target.parent.mkdir(parents=True, exist_ok=True)
            if local.is_dir():
                shutil.copytree(local, target)
            else:
                shutil.copy2(local, target)
            tree = self._devicectl([*copy, "--source", str(root), "--destination", f"{parts[0]}/{parts[1]}"], DEVICECTL_TIMEOUT_SEC)
        return self._outcome(tree, f"push to {destination} failed")

    def pull(self, device: DeviceInfo, remote: str, local: Path) -> StepOutcome:
        app = self._require_app()
        if not app:
            return StepOutcome(constants.BLOCKED, "pull needs the application id (bind_application was not called)")
        local.parent.mkdir(parents=True, exist_ok=True)
        result = self._devicectl(
            ["device", "copy", "from", "--device", udid_of(device), "--domain-type", "appDataContainer", "--domain-identifier", app,
             "--source", self._container_path(remote), "--destination", str(local)], DEVICECTL_TIMEOUT_SEC)
        if not result.ok and result.status == constants.FAIL and _NOT_FOUND.search(result.output):
            # Nothing there yet (no run folder before the first launch): an empty result, not a failure.
            local.mkdir(parents=True, exist_ok=True)
            return StepOutcome(constants.PASS, details={"note": f"{remote} does not exist on the device"})
        return self._outcome(result, f"pull of {remote} failed")

    # -- run control ---------------------------------------------------------------------------------------------------------

    def launch(self, device: DeviceInfo, app: str, args: Sequence[str], log: Path) -> StepOutcome:
        self._app = app
        udid = udid_of(device)
        self._stop_console(udid)
        log.parent.mkdir(parents=True, exist_ok=True)
        handle = open(log, "wb")  # truncated: a "Launched application" line of an earlier launch must not count for this one
        argv = ["xcrun", "devicectl", "device", "process", "launch", "--device", udid, "--terminate-existing", "--console", app, *args]
        try:
            process, tree = self._spawn(argv, None, _tool_env(), handle)
        except OSError as error:
            handle.close()
            return StepOutcome(constants.BLOCKED, f"cannot start devicectl: {error}")
        except BaseException:
            handle.close()
            raise
        self._consoles[udid] = (process, tree, handle)
        deadline = time.monotonic() + LAUNCH_WAIT_SEC
        while time.monotonic() < deadline:
            text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
            if "Launched application" in text:
                return StepOutcome(constants.PASS, details={"layer.launch": constants.PASS})
            if process.poll() is not None:
                self._stop_console(udid)
                status = constants.NEEDS_PERMISSION if _LOCKED.search(text) else constants.FAIL
                tail = " ".join(text.strip().splitlines()[-3:])[:300]
                return StepOutcome(status, ("the device is locked; unlock it (owner action): " if status == constants.NEEDS_PERMISSION
                                            else "devicectl launch exited: ") + tail)
            self._sleep(0.5)
        self._stop_console(udid)
        return StepOutcome(constants.TIMEOUT, f"the application did not report a launch within {LAUNCH_WAIT_SEC:g} s")

    def _stop_console(self, udid: str) -> bool:
        entry = self._consoles.pop(udid, None)
        if entry is None:
            return False
        process, tree, handle = entry
        try:
            if tree.alive():
                tree.kill()
            if process.poll() is None:
                process.wait(timeout=30)
        except Exception:  # noqa: BLE001 - best effort; the next launch uses --terminate-existing
            pass
        finally:
            tree.close()
            handle.close()
        return True

    def terminate(self, device: DeviceInfo, app: str) -> StepOutcome:
        udid = udid_of(device)
        stopped_console = self._stop_console(udid)
        pids = self._app_pids(udid, app)
        if pids is None:
            return StepOutcome(constants.FAIL, "the console was stopped but the device could not be asked whether the application "
                                               "still runs (it may still be running)")
        notes = []
        for pid in pids:
            result = self._devicectl(["device", "process", "terminate", "--device", udid, "--pid", str(pid)], 60)
            if not result.ok:
                notes.append(f"pid {pid}: {(result.reason or result.output).strip()[:120]}")
        if notes:
            return StepOutcome(constants.FAIL, "the application process could not be terminated: " + "; ".join(notes))
        if pids:  # a PASS is verified, not assumed; SIGTERM leaves the process listed for a moment, so poll briefly
            still = self._app_pids(udid, app)
            for _ in range(5):
                if still == []:
                    break
                self._sleep(1.0)
                still = self._app_pids(udid, app)
            if still is None or still:
                return StepOutcome(constants.FAIL, "the application is still running after terminate"
                                   if still else "the device could not confirm that the application stopped")
        return StepOutcome(constants.PASS, details={"note": "console stopped" if stopped_console else "no console was attached"})

    def _app_pids(self, udid: str, app: str) -> list[int] | None:
        """Process ids of the application, `[]` when it is not running, `None` when the device could not be asked."""
        apps = self._devicectl_json(["device", "info", "apps", "--device", udid, "--bundle-id", app], 60)
        processes = self._devicectl_json(["device", "info", "processes", "--device", udid], 60)
        if apps is None or processes is None:
            return None
        bundle = next((a.get("url") for a in (_get(apps, "result", "apps") or []) if a.get("bundleIdentifier") == app), None)
        if not bundle:
            return []
        prefix = _device_path(str(bundle)).rstrip("/")
        return [p["processIdentifier"] for p in _get(processes, "result", "runningProcesses") or []
                if isinstance(p.get("processIdentifier"), int) and _device_path(str(p.get("executable", ""))).startswith(prefix)]

    def screenshot(self, device: DeviceInfo, out: Path) -> StepOutcome:
        out.parent.mkdir(parents=True, exist_ok=True)
        result = self._devicectl(["device", "capture", "screenshot", "--device", udid_of(device), "--destination", str(out)], 90)
        outcome = self._outcome(result, "screenshot failed")
        if outcome.ok and not out.is_file():
            return StepOutcome(constants.FAIL, f"devicectl reported success but wrote no {out.name}")
        return outcome

    def health(self, device: DeviceInfo) -> DeviceHealth:
        udid = udid_of(device)
        try:
            connected = any(d.id == device.id for d in self.discover())
        except Exception as error:  # noqa: BLE001
            return DeviceHealth(False, notes=(f"discovery failed: {error}",))
        if not connected:
            return DeviceHealth(False, notes=("the device is not connected and paired",))
        lock = self._devicectl_json(["device", "info", "lockState", "--device", udid], 60)
        if lock is None:
            return DeviceHealth(True, notes=("the lock state could not be read",))
        required = _get(lock, "result", "passcodeRequired")
        return DeviceHealth(True, locked=bool(required) if isinstance(required, bool) else None)

    # -- devicectl -----------------------------------------------------------------------------------------------------------

    def _devicectl(self, arguments: Sequence[str], timeout_sec: float) -> supervise.BoundedResult:
        """One bounded `xcrun devicectl` call: devicectl gets its own `--timeout`, the supervisor kills it at `timeout_sec`. The stall
        window equals the timeout because devicectl prints nothing while a large install or copy runs."""
        return self._run(["xcrun", "devicectl", *arguments, "--timeout", str(int(max(timeout_sec - 5, 5)))], timeout_sec,
                         timeout_sec, env=_tool_env())

    def _devicectl_json(self, arguments: Sequence[str], timeout_sec: float) -> dict | None:
        with tempfile.TemporaryDirectory(prefix="gdv-devicectl-") as folder:
            target = Path(folder) / "out.json"
            result = self._devicectl([*arguments, "--json-output", str(target)], timeout_sec)
            if not result.ok:
                return None
            try:
                document = json.loads(target.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                return None
        return document if isinstance(document, dict) else None

    @staticmethod
    def _outcome(result: supervise.BoundedResult, failure: str, details: Mapping[str, str] | None = None) -> StepOutcome:
        if result.ok:
            return StepOutcome(constants.PASS, details=dict(details or {}))
        text = (result.output or "").strip()
        if result.status == constants.FAIL and _LOCKED.search(text):
            return StepOutcome(constants.NEEDS_PERMISSION, f"{failure}: the device is locked; unlock it (owner action)")
        detail = result.reason or " ".join(text.splitlines()[-2:])[:200]
        return StepOutcome(result.status, f"{failure}: {detail}", details=dict(details or {}))


register(PLATFORM, IosAdapter)
