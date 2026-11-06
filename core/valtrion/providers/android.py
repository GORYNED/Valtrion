"""Android adapter: a physical device (or an emulator, labelled as such) driven through `adb`.

Flow of a run: `build` (Unity batchmode, a consumer `-executeMethod` hook and a profile from the project's
`validation/device/profiles.json`), `install` (`adb install -r`), `prepare` (binds the application; `pm clear` only for a
`clean-install` plan), `push` of the plan into the application's data root (`/sdcard/Android/data/<package>/files`, which is
`persistentDataPath`), `launch` (`am start` with the `gdv-plan` intent extra, `logcat` captured), `pull` polls of `gdv/out`,
`terminate` (dumpsys snapshots, `am force-stop`, logcat stopped) and `screenshot`.

An emulator is labelled `emulator` and a plan of kind `performance` refuses it. A device that is `unauthorized` or `offline` is
NEEDS_PERMISSION with the action for the owner; a missing device or tool is BLOCKED. Every `adb` call is bounded by the
supervisor, so a hung `adb` cannot hang a run. The adapter never changes device settings and never uninstalls or clears an
application outside a `clean-install` plan.

`profiles.json` (`<unity project>/validation/device/profiles.json`):

    {"profiles": {"Validation": {"android": {
        "executeMethod": "Company.Project.Editor.Build.Android", "scriptingBackend": "Mono",
        "package": "com.company.project", "activity": "com.unity3d.player.UnityPlayerGameActivity",
        "timeoutSec": 3600, "stallSec": 600, "extraArgs": []}}}}

The hook receives `-buildOutput <apk path>`, `-gdvProfile <name>` and `-gdvBackend <Mono|IL2CPP>`. Mono is for smoke plans;
a performance plan names a profile with `"scriptingBackend": "IL2CPP"`. `GDV_ANDROID_BACKEND` overrides the profile.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .. import config, constants, core, doctor, supervise
from ..adapter import Capabilities, DeviceHealth, DeviceInfo, PlatformAdapter, StepOutcome
from . import register

ADB_ENV = "GDV_ADB"
PLATFORM = "android"
DEVICE_ID_PREFIX = "android:"
DATA_ROOT = "/sdcard/Android/data"
REMOTE_SCRATCH = "/data/local/tmp"
LAUNCH_NOTES = "launch.log"  # inside the diagnostics folder: what a slow start saw, poll by poll
DIAGNOSTICS = "android"  # folder inside a run folder: logcat, dumpsys snapshots, health records
CALL_TIMEOUT_SEC = 60.0
INSTALL_TIMEOUT_SEC = 900.0
PULL_TIMEOUT_SEC = 180.0
DEFAULT_BUILD_TIMEOUT_SEC = 3600.0
DEFAULT_BUILD_STALL_SEC = 600.0
UNITY_LOG_TAGS = ("Unity:V", "AndroidRuntime:E", "DEBUG:V", "ActivityManager:I", "*:S")
SERVER_RETRIES = 4
_EMULATOR_SERIALS = ("emulator-", "127.0.0.1:", "localhost:", "0.0.0.0:")
_EMULATOR_PRODUCTS = ("generic", "emu", "sdk_", "cutf", "vsoc")
_SERVER_DOWN = ("cannot connect to daemon", "failed to start daemon", "could not read ok from ADB Server")
_THERMAL = {0: "none", 1: "light", 2: "moderate", 3: "severe", 4: "critical", 5: "emergency", 6: "shutdown"}
_NOT_READY = {"unauthorized": "unauthorized", "offline": "offline", "authorizing": "unauthorized"}
_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")
_ACTIVITY = re.compile(r"^[A-Za-z0-9_.$]+$")
_REMOTE_PATH = re.compile(r"^/?[A-Za-z0-9._=+@-]+(/[A-Za-z0-9._=+@-]+)*/?$")
_KNOWN_STATES = ("device", "offline", "unauthorized", "authorizing", "recovery", "sideload", "bootloader", "host", "rescue")
LAUNCH_POLL_SEC = 2.0  # between two looks at a slow start (process, resumed activity, logcat)
LAUNCH_POLL_CALL_SEC = 20.0  # bound of each adb call of such a look, so a hung adb overshoots the launch wait by seconds, not minutes
STATE_SETTLE_SEC = 10.0  # a device is `offline` or `authorizing` for a few seconds after the shared server restarts


def quote(text: str) -> str:
    """One argument for the device shell: single-quoted, so nothing in it is expanded or split."""
    return "'" + text.replace("'", "'\\''") + "'"


def find_adb(environ: Mapping[str, str] | None = None, extra_dirs: Sequence[str] | None = None) -> list[str] | None:
    """The adb command: `GDV_ADB`, then `adb` on PATH or in the usual install folders (the same search `doctor` uses), then the copy
    bundled with a Unity Android module."""
    environment = os.environ if environ is None else environ
    override = environment.get(ADB_ENV)
    if override:
        return [override]
    found = doctor.which_tool("adb", core.local_os(), extra_dirs)
    if found:
        return [found]
    for editor in sorted(doctor.find_editors(core.local_os(), dict(environment)), key=lambda e: e["version"], reverse=True):
        if editor.get("adb"):
            return [editor["adb"]]
    return None


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def parse_devices(output: str) -> list[dict]:
    """Lines of `adb devices -l` after its header: `{serial, state, model}`. Notices printed before the header (a restarted
    server, a version mismatch) and lines in an unknown state are never taken for devices."""
    devices = []
    started = False
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("List of devices"):
            started = True
            continue
        if not started or not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2 or parts[1] not in _KNOWN_STATES:
            continue
        fields = dict(part.split(":", 1) for part in parts[2:] if ":" in part)
        devices.append({"serial": parts[0], "state": parts[1], "model": fields.get("model", "")})
    return devices


def parse_getprop(output: str) -> dict[str, str]:
    return {key: value for key, value in re.findall(r"^\[([^\]]+)\]: \[(.*)\]\s*$", output, re.MULTILINE)}


def split_app(app: str) -> tuple[str, str | None]:
    """`package` or `package/activity` (the activity may be written `.Name` or fully qualified)."""
    package, _, activity = app.partition("/")
    return package, activity or None


@dataclass
class _LogSession:
    trees: list = field(default_factory=list)  # (process, tree) of each logcat
    handles: list = field(default_factory=list)

    def ended_early(self) -> bool:
        return any(process.poll() is not None for process, _ in self.trees)

    def stop(self) -> None:
        for process, tree in self.trees:
            tree.kill()
            try:
                process.wait(timeout=10)
            except Exception:  # noqa: BLE001 - a logcat that will not exit is already killed; do not fail the run for it
                pass
            tree.close()
        for handle in self.handles:
            handle.close()


class AndroidAdapter(PlatformAdapter):
    platform = PLATFORM
    supports_launch_wait = True

    def __init__(self, adb: Sequence[str] | None = None, unity_editors: Callable[[], Sequence[dict]] | None = None,
                 sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
                 unity_command: Callable[[dict], list[str]] = lambda editor: [editor["path"]]) -> None:
        self._adb_command = list(adb) if adb is not None else None
        self._unity_editors = unity_editors or (lambda: doctor.find_editors(core.local_os()))
        self._unity_command = unity_command
        self._sleep = sleep
        self._monotonic = monotonic
        self._launch_timeout: float = float(constants.LAUNCH_TIMEOUT_DEFAULT_SEC)
        self._app: tuple[str, str | None] | None = None
        self._sessions: dict[str, _LogSession] = {}
        self._diagnostics: dict[str, Path] = {}
        self._lock = threading.Lock()
        self._server_started = False
        self._launches: dict[str, int] = {}  # serial -> token of the newest launch; an abandoned older launch must not stop its logcat
        self._closed: set[str] = set()  # serials terminated; an abandoned launch must not start a capture for them

    # -- adb ------------------------------------------------------------------------------------------------------------

    def adb_command(self) -> list[str] | None:
        if self._adb_command is None:
            self._adb_command = find_adb()
        return self._adb_command

    def _adb(self, serial: str | None, *args: str, timeout: float = CALL_TIMEOUT_SEC, stall: float | None = None,
             log_path: Path | None = None) -> supervise.BoundedResult:
        command = self.adb_command()
        if command is None:
            return supervise.BoundedResult(constants.BLOCKED, "adb is not available: not on PATH and not bundled with a Unity "
                                           f"Android module (set {ADB_ENV})", None, "")
        self._start_server(command)
        argv = [*command, *(["-s", serial] if serial else []), *args]
        for attempt in range(SERVER_RETRIES):
            result = supervise.run_bounded(argv, timeout, stall_sec=stall if stall is not None else timeout, log_path=log_path,
                                           poll_sec=0.05)
            # Another adb of a different version on this machine restarts the shared server; the next call finds it running.
            if result.returncode in (None, 0) or not any(text in result.output for text in _SERVER_DOWN) or attempt == SERVER_RETRIES - 1:
                return result
            self._server_started = False  # start it again outside the job; a server auto-started by this call would die with it
            self._start_server(command)
            self._sleep(2.0)
        return result

    def _start_server(self, command: Sequence[str]) -> None:
        """Start the shared adb server once, outside the supervised job. A server auto-started by a supervised call is a child of
        that call, so the Windows job (or the process group) that ends the call would kill the server and every logcat on it."""
        if self._server_started:
            return
        self._server_started = True
        try:
            subprocess.run([*command, "start-server"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30, check=False, creationflags=constants.NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass  # the first supervised call reports what is wrong

    def _shell(self, serial: str, script: str, timeout: float = CALL_TIMEOUT_SEC) -> supervise.BoundedResult:
        return self._adb(serial, "shell", script, timeout=timeout)

    @staticmethod
    def _serial(device: DeviceInfo) -> str:
        return device.id[len(DEVICE_ID_PREFIX):] if device.id.startswith(DEVICE_ID_PREFIX) else device.id

    @staticmethod
    def classify(result: supervise.BoundedResult, serial: str, what: str) -> StepOutcome:
        """Turn a failed adb call into the contract status with the action the owner must take."""
        if result.returncode == 0:
            return StepOutcome(constants.PASS)
        if result.returncode is None:  # adb never ran to an exit: missing, timed out or stalled
            return StepOutcome(result.status, f"{what}: {result.reason}")
        text = result.output.strip()
        lowered = text.lower()
        if re.search(r"\bdevice (?:'[^']*' )?unauthorized\b", lowered):
            return StepOutcome(constants.NEEDS_PERMISSION, f"device {serial} is unauthorized: unlock the phone and accept the "
                               "'Allow USB debugging' prompt (owner action)")
        if re.search(r"\bdevice (?:'[^']*' )?offline\b", lowered):
            return StepOutcome(constants.NEEDS_PERMISSION, f"device {serial} is offline: reconnect the cable, unlock the phone "
                               "and confirm USB debugging (owner action)")
        if re.search(r"\bdevice '[^']*' not found", lowered) or "no devices/emulators found" in lowered:
            return StepOutcome(constants.BLOCKED, f"device {serial} is not attached ({text[:160]})")
        return StepOutcome(constants.FAIL, f"{what} failed (exit {result.returncode}): {text[-300:]}")

    def _device_state(self, serial: str) -> tuple[str | None, supervise.BoundedResult]:
        waited = 0.0
        while True:
            listing = self._adb(None, "devices", "-l", timeout=CALL_TIMEOUT_SEC)
            if not listing.ok or listing.returncode != 0:
                return None, listing
            state = next((e["state"] for e in parse_devices(listing.output) if e["serial"] == serial), None)
            # `offline` and `authorizing` are transient for a few seconds after the shared adb server restarts.
            if state not in ("offline", "authorizing") or waited >= STATE_SETTLE_SEC:
                return state, listing
            self._sleep(2.0)
            waited += 2.0

    def _ready(self, device: DeviceInfo, what: str) -> StepOutcome:
        """PASS when the device is attached and authorized; otherwise the status that says what to do."""
        serial = self._serial(device)
        state, listing = self._device_state(serial)
        if state is None:
            if not listing.ok or listing.returncode != 0:
                return self.classify(listing, serial, "adb devices")
            return StepOutcome(constants.BLOCKED, f"device {serial} is not attached ({what})")
        if state in _NOT_READY:
            return self.classify(supervise.BoundedResult(constants.PASS, None, 1, f"device {_NOT_READY[state]}"), serial, what)
        if state != "device":
            return StepOutcome(constants.BLOCKED, f"device {serial} is in state '{state}' ({what})")
        return StepOutcome(constants.PASS)

    # -- probe and discovery ----------------------------------------------------------------------------------------------

    def probe(self) -> Capabilities:
        available: list[str] = []
        blocked: dict[str, str] = {}
        command = self.adb_command()
        if command is None:
            blocked["adb"] = f"adb is not on PATH and no Unity Android module bundles it (set {ADB_ENV})"
        else:
            version = self._adb(None, "version", timeout=20)
            if version.ok and version.returncode == 0:
                available.append("adb")
            else:
                blocked["adb"] = f"adb is not usable: {version.reason or version.output.strip()[:120]}"
        editors = [e for e in self._unity_editors() if "android" in e.get("modules", ())]
        if editors:
            available.append("build")
        else:
            blocked["build"] = "no Unity editor with the Android module (SDK, NDK, OpenJDK) is installed"
        return Capabilities(PLATFORM, tuple(available), blocked)

    def discover(self) -> Sequence[DeviceInfo]:
        listing = self._adb(None, "devices", "-l")
        if listing.status in (constants.BLOCKED,) and self.adb_command() is None:
            return []  # no adb: nothing can be attached to this host; `probe` says why
        if not listing.ok or listing.returncode != 0:
            raise RuntimeError(f"adb devices failed: {listing.reason or listing.output.strip()[:200]}")
        found = []
        for entry in parse_devices(listing.output):
            found.append(self._describe(entry))
        return found

    def _describe(self, entry: dict) -> DeviceInfo:
        serial, state = entry["serial"], entry["state"]
        device_id = DEVICE_ID_PREFIX + serial
        emulator = serial.startswith(_EMULATOR_SERIALS)
        if state != "device":
            capability = "adb." + _NOT_READY.get(state, state)
            model = entry["model"].replace("_", " ")
            return DeviceInfo(device_id, PLATFORM, "android-emulator" if emulator else "android-" + (slug(model) or "unknown"),
                              constants.EVIDENCE_EMULATOR if emulator else constants.EVIDENCE_PHYSICAL, model, "", ("touch", "needs-permission", capability))
        properties = parse_getprop(self._adb(serial, "shell", "getprop", timeout=CALL_TIMEOUT_SEC).output)
        model = properties.get("ro.product.model", entry["model"].replace("_", " "))
        release = properties.get("ro.build.version.release", "")
        abi = properties.get("ro.product.cpu.abi", "")
        if not properties:  # getprop answered nothing: the device cannot be told from an emulator, so it is not physical evidence
            if emulator:
                return DeviceInfo(device_id, PLATFORM, "android-emulator", constants.EVIDENCE_EMULATOR, model, "", ("touch",))
            return DeviceInfo(device_id, PLATFORM, "android-unknown", constants.EVIDENCE_HOST, model, "", ("touch",))
        emulator = (emulator or properties.get("ro.kernel.qemu") == "1"
                    or properties.get("ro.boot.qemu") == "1" or "emulator" in properties.get("ro.build.characteristics", "")
                    or any(properties.get(key) in ("goldfish", "ranchu", "vbox86", "cutf_cvm") for key in ("ro.hardware", "ro.boot.hardware"))
                    or any(properties.get(key, "").startswith(_EMULATOR_PRODUCTS) for key in ("ro.product.device", "ro.product.name", "ro.build.product"))
                    or properties.get("ro.product.manufacturer", "").lower() == "genymotion")
        capabilities = ["touch"] + ([f"abi.{abi}"] if abi else [])
        if emulator:
            return DeviceInfo(device_id, PLATFORM, "android-emulator", constants.EVIDENCE_EMULATOR, model,
                              f"Android {release}".strip(), tuple(capabilities))
        return DeviceInfo(device_id, PLATFORM, "android-" + (slug(model) or "unknown"), constants.EVIDENCE_PHYSICAL, model,
                          f"Android {release}".strip(), tuple(capabilities))

    # -- health -----------------------------------------------------------------------------------------------------------

    def health(self, device: DeviceInfo) -> DeviceHealth:
        serial = self._serial(device)
        state, listing = self._device_state(serial)
        if state is None:
            reason = listing.reason if not listing.ok else "not attached"
            return DeviceHealth(False, notes=(f"device {serial} {reason}",))
        if state in _NOT_READY:
            outcome = self.classify(supervise.BoundedResult(constants.PASS, None, 1, f"device {_NOT_READY[state]}"), serial, "health")
            return DeviceHealth(False, notes=(outcome.reason or state,), permission_needed=outcome.reason)
        if state != "device":
            return DeviceHealth(False, notes=(f"device {serial} is in state '{state}'",))
        snapshot = self._snapshot(serial)
        notes = list(snapshot["notes"])
        locked = self._keyguard_showing(serial)
        if locked is None:
            notes.append("lock state not reported by dumpsys")
        if locked:
            # Wake a sleeping screen and dismiss an insecure keyguard; a secure lock stays and is the owner's to open.
            self._adb(serial, "shell", "input keyevent KEYCODE_WAKEUP; wm dismiss-keyguard", timeout=CALL_TIMEOUT_SEC)
            self._sleep(1.0)
            locked = self._keyguard_showing(serial)
        return DeviceHealth(True, locked, snapshot["thermal"], snapshot["battery"], snapshot["freeMb"], tuple(notes))

    def _keyguard_showing(self, serial: str) -> bool | None:
        reply = self._shell(serial, "dumpsys window | grep -E 'isKeyguardShowing'; dumpsys window policy | grep -E 'showing='")
        if reply.returncode not in (0, 1):
            return None
        values = re.findall(r"\b(?:isKeyguardShowing|showing)=(true|false)", reply.output)
        return None if not values else "true" in values  # no key at all means the state is unknown, not unlocked

    def _snapshot(self, serial: str) -> dict:
        """Battery, thermal status and free space as `dumpsys`/`df` report them. Unknown values stay None."""
        script = ("dumpsys battery; echo '--thermal--'; dumpsys thermalservice | grep -E 'Thermal Status|mStatus'; "
                  "echo '--df--'; df -k /sdcard")
        reply = self._shell(serial, script)
        data: dict = {"battery": None, "thermal": None, "freeMb": None, "notes": [], "raw": reply.output}
        if not reply.ok or reply.returncode != 0:
            data["notes"].append(f"health snapshot failed: {reply.reason or reply.output.strip()[:120]}")
            return data
        battery, _, rest = reply.output.partition("--thermal--")
        thermal, _, disk = rest.partition("--df--")
        level = re.search(r"^\s*level:\s*(\d+)", battery, re.MULTILINE)
        if level:
            data["battery"] = int(level.group(1))
        temperature = re.search(r"^\s*temperature:\s*(-?\d+)", battery, re.MULTILINE)
        status = re.search(r"^\s*status:\s*(\d+)", battery, re.MULTILINE)
        if temperature:
            data["notes"].append(f"battery temperature {int(temperature.group(1)) / 10:.1f} C")
        if status:
            data["notes"].append(f"battery status code {status.group(1)}")
        state = re.search(r"Thermal Status:\s*(\d+)", thermal) or re.search(r"mStatus:?\s*(\d+)", thermal)
        if state:
            data["thermal"] = _THERMAL.get(int(state.group(1)), f"status {state.group(1)}")
        free = re.search(r"^\S+\s+\d+\s+\d+\s+(\d+)\s+\d+%", disk, re.MULTILINE)
        if free:
            data["freeMb"] = int(free.group(1)) // 1024
        return data

    # -- build, install, prepare ---------------------------------------------------------------------------------------------

    def toolchain(self) -> dict[str, str]:
        """What changes the build besides the project's profile (whose settings are in the build cache key already): the
        `GDV_ANDROID_BACKEND` override, so a Mono build and an IL2CPP build of one commit get different keys."""
        backend = os.environ.get("GDV_ANDROID_BACKEND")
        return {"androidBackend": backend} if backend else {}

    def build(self, project: Path, profile: str, out: Path) -> StepOutcome:
        profiles_path = project / "validation" / "device" / "profiles.json"
        try:
            document = json.loads(profiles_path.read_text(encoding="utf-8-sig"))
        except OSError as error:
            return StepOutcome(constants.BLOCKED, f"no build profiles: cannot read {profiles_path} ({error.strerror or error})")
        except ValueError as error:
            return StepOutcome(constants.BLOCKED, f"{profiles_path} is not valid JSON: {error}")
        entry = (document.get("profiles", {}).get(profile) or {}).get(PLATFORM) if isinstance(document, dict) else None
        if not isinstance(entry, dict) or not entry.get("executeMethod"):
            return StepOutcome(constants.BLOCKED, f"{profiles_path} has no android build for profile '{profile}' "
                               "(profiles.<name>.android.executeMethod is required)")
        package = str(entry.get("package", ""))
        if not _PACKAGE.fullmatch(package):
            return StepOutcome(constants.BLOCKED, f"profile '{profile}' needs a valid android.package, got {package!r}")
        activity = entry.get("activity")
        if activity is not None and not _ACTIVITY.fullmatch(str(activity)):
            return StepOutcome(constants.BLOCKED, f"profile '{profile}' has an invalid android.activity {activity!r}")
        backend = os.environ.get("GDV_ANDROID_BACKEND") or str(entry.get("scriptingBackend", "Mono"))
        if backend not in ("Mono", "IL2CPP"):
            return StepOutcome(constants.BLOCKED, f"scriptingBackend must be Mono or IL2CPP, got {backend!r}")
        editor, why = self._editor_for(project)
        if editor is None:
            return StepOutcome(constants.BLOCKED, why or "no Unity editor")
        out.mkdir(parents=True, exist_ok=True)
        apk = out / f"{core.safe_segment(package)}.apk"
        logs = out.parent / "build-logs"  # outside `out`: the pipeline removes `out` after the run, and a failed build needs its log
        logs.mkdir(parents=True, exist_ok=True)
        log = logs / "unity-build.log"
        argv = [*self._unity_command(editor), "-batchmode", "-quit", "-nographics", "-projectPath", str(project), *config.unity_accelerator_args(), "-buildTarget", "Android",
                "-logFile", str(log), "-executeMethod", str(entry["executeMethod"]), "-buildOutput", str(apk),
                "-gdvProfile", profile, "-gdvBackend", backend, *[str(a) for a in entry.get("extraArgs", [])]]
        timeout = float(entry.get("timeoutSec", DEFAULT_BUILD_TIMEOUT_SEC))
        stall = float(entry.get("stallSec", DEFAULT_BUILD_STALL_SEC))
        outcome = supervise.supervise(argv, supervise.Watchdog(timeout, stall, progress_files=(log,), cpu_counts=True, log_counts=False),
                                      logs / "unity-process.log")
        layers = {"layer.compile": outcome.status if outcome.status != constants.PASS else constants.PASS}
        if outcome.status != constants.PASS:
            return StepOutcome(outcome.status, f"Unity Android build: {outcome.reason} (log: {log})", details=layers)
        if not apk.is_file():
            return StepOutcome(constants.FAIL, f"Unity exited 0 but wrote no {apk.name} (the -executeMethod hook must write "
                               "the apk to -buildOutput)", details={"layer.compile": constants.PASS, "layer.nativeBuild": constants.FAIL})
        self._app = (package, str(activity) if activity else None)
        return StepOutcome(constants.PASS, artifacts=(apk,), details={
            "layer.compile": constants.PASS, "layer.nativeBuild": constants.PASS, "package": package, "backend": backend})

    def _editor_for(self, project: Path) -> tuple[dict | None, str | None]:
        version = None
        try:
            match = re.search(r"m_EditorVersion:\s*(\S+)", (project / "ProjectSettings" / "ProjectVersion.txt").read_text(encoding="utf-8"))
            version = match.group(1) if match else None
        except OSError:
            pass
        if version is None:
            return None, f"cannot read the Unity version from {project / 'ProjectSettings' / 'ProjectVersion.txt'}"
        for editor in self._unity_editors():
            if editor["version"] == version:
                if "android" not in editor.get("modules", ()):
                    return None, f"Unity {version} has no Android module installed"
                return editor, None
        return None, f"Unity {version} is not installed"

    def install(self, device: DeviceInfo, artifact: Path) -> StepOutcome:
        apk = artifact
        if apk.is_dir():
            apks = sorted(apk.glob("*.apk"))
            if len(apks) != 1:
                return StepOutcome(constants.BLOCKED, f"{apk} holds {len(apks)} apk files; name the one to install")
            apk = apks[0]
        if not apk.is_file():
            return StepOutcome(constants.BLOCKED, f"install refused: {apk} does not exist")
        serial = self._serial(device)
        ready = self._ready(device, "install")
        if not ready.ok:
            return ready
        reply = self._adb(serial, "install", "-r", str(apk), timeout=INSTALL_TIMEOUT_SEC)
        outcome = self.classify(reply, serial, "adb install")
        if outcome.status in (constants.TIMEOUT, constants.STALL):
            return StepOutcome(outcome.status, f"{outcome.reason}; a confirmation prompt on the phone (Install via USB) may be waiting "
                               "for the owner")
        if outcome.ok and "Failure [" in reply.output:  # an old adb exits 0 after printing the failure
            outcome = StepOutcome(constants.FAIL, f"adb install failed: {reply.output.strip()[-300:]}")
        conflict = "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in reply.output or "INSTALL_FAILED_VERSION_DOWNGRADE" in reply.output
        if not outcome.ok and outcome.status == constants.FAIL and conflict:
            return StepOutcome(constants.FAIL, f"{outcome.reason} (the installed build is signed differently or newer; uninstalling "
                               "it would delete its data, so the owner decides)")
        if not outcome.ok and outcome.status == constants.FAIL and any(
                text in reply.output for text in ("INSTALL_FAILED_USER_RESTRICTED", "User rejected", "INSTALL_FAILED_ABORTED")):
            return StepOutcome(constants.NEEDS_PERMISSION, f"the phone refused the install ({reply.output.strip()[-200:]}): allow "
                               "'Install via USB' in the developer options and accept the prompt on the phone (owner action)")
        if not outcome.ok:
            return outcome
        if "Success" not in reply.output:
            return StepOutcome(constants.FAIL, f"adb install did not report Success: {reply.output.strip()[-300:]}")
        return StepOutcome(constants.PASS, details={"apk": str(apk)})

    def prepare(self, device: DeviceInfo, app: str, data_policy: str | None) -> StepOutcome:
        package, activity = split_app(app)
        if not _PACKAGE.fullmatch(package) or (activity is not None and not _ACTIVITY.fullmatch(activity)):
            return StepOutcome(constants.BLOCKED, f"'{app}' is not a package or package/activity")
        self._app = (package, activity or (self._app[1] if self._app and self._app[0] == package else None))
        if data_policy != "clean-install":
            return StepOutcome(constants.PASS)
        serial = self._serial(device)
        installed = self._shell(serial, f"pm list packages {package}")
        if installed.ok and f"package:{package}" not in installed.output.split():
            return StepOutcome(constants.PASS, details={"cleared": "false"})  # not installed yet: nothing to clear
        cleared = self._shell(serial, f"pm clear {package}")
        outcome = self.classify(cleared, serial, "pm clear")
        if outcome.ok and "Success" not in cleared.output:
            return StepOutcome(constants.FAIL, f"pm clear did not report Success: {cleared.output.strip()[-200:]}")
        return outcome

    # -- files ------------------------------------------------------------------------------------------------------------

    def _remote(self, remote: str) -> tuple[str | None, str | None]:
        """Absolute device path for a path relative to the application's data root (persistentDataPath)."""
        if not _REMOTE_PATH.fullmatch(remote) or ".." in remote.split("/"):
            return None, f"'{remote}' is not a plain device path (letters, digits and ._=+@- in segments, no '..')"
        if remote.startswith("/"):
            return remote, None
        if self._app is None:
            return None, "the application is not bound: nothing says which package's data root to use"
        return f"{DATA_ROOT}/{self._app[0]}/files/{remote.strip('/')}", None

    def push(self, device: DeviceInfo, local: Path, remote: str) -> StepOutcome:
        target, problem = self._remote(remote)
        if target is None:
            return StepOutcome(constants.BLOCKED, problem or "no target")
        if not local.exists():
            return StepOutcome(constants.BLOCKED, f"{local} does not exist")
        serial = self._serial(device)
        ready = self._ready(device, "push")
        if not ready.ok:
            return ready
        parent = target.rsplit("/", 1)[0] if local.is_file() else target
        # A folder created by `adb shell` belongs to the shell user and the application cannot write into it (it could not even
        # create `gdv/out`), so the folders created here are opened with the setgid bit, which also lets the application's own
        # subfolders inherit the group the shell can read. The chmod of an existing application-owned folder fails and is ignored.
        chain, root = [], f"{DATA_ROOT}/{self._app[0]}/files" if self._app else ""
        if root and parent.startswith(root + "/"):
            walk = root
            for part in parent[len(root) + 1:].split("/"):
                walk += "/" + part
                chain.append(quote(walk))
        made = self._shell(serial, f"mkdir -p {quote(parent)} && {{ chmod 2777 {' '.join(chain)} 2>/dev/null; true; }}" if chain
                           else f"mkdir -p {quote(parent)}")
        if not (made.ok and made.returncode == 0):
            return self.classify(made, serial, "mkdir")
        source = str(local) if local.is_file() else str(local) + os.sep + "."
        return self.classify(self._adb(serial, "push", source, target, timeout=PULL_TIMEOUT_SEC), serial, "adb push")

    def pull(self, device: DeviceInfo, remote: str, local: Path) -> StepOutcome:
        source, problem = self._remote(remote)
        if source is None:
            return StepOutcome(constants.BLOCKED, problem or "no source")
        serial = self._serial(device)
        path = quote(source)
        kind = self._shell(serial, f"if [ -d {path} ]; then echo dir; elif [ -e {path} ]; then echo file; else echo absent; fi")
        if not (kind.ok and kind.returncode == 0):
            return self.classify(kind, serial, "adb shell")
        word = kind.output.strip().split()[-1] if kind.output.strip() else ""
        if word == "absent":
            local.mkdir(parents=True, exist_ok=True)
            return StepOutcome(constants.PASS, details={"absent": "true"})  # nothing there yet is not an error
        if word == "dir":
            local.mkdir(parents=True, exist_ok=True)
            reply = self._adb(serial, "pull", source.rstrip("/") + "/.", str(local), timeout=PULL_TIMEOUT_SEC)
        else:
            local.parent.mkdir(parents=True, exist_ok=True)
            reply = self._adb(serial, "pull", source, str(local), timeout=PULL_TIMEOUT_SEC)
        return self.classify(reply, serial, "adb pull")

    # -- run --------------------------------------------------------------------------------------------------------------

    def _resolve_activity(self, serial: str, package: str, activity: str | None) -> tuple[str | None, str | None]:
        if activity:
            return (package + activity if activity.startswith(".") else activity), None
        reply = self._shell(serial, f"cmd package resolve-activity --brief -c android.intent.category.LAUNCHER {package}")
        lines = [line.strip() for line in reply.output.splitlines() if "/" in line and not line.startswith("No activity")]
        if not (reply.ok and reply.returncode == 0 and lines):
            return None, f"{package} has no launcher activity on the device (is it installed?)"
        component = lines[-1].split("/", 1)[1]
        if not _ACTIVITY.fullmatch(component):
            return None, f"the launcher activity the device reported ('{component[:80]}') is not a class name"
        return (package + component if component.startswith(".") else component), None

    def bind_launch_timeout(self, seconds: float) -> None:
        self._launch_timeout = seconds

    def launch(self, device: DeviceInfo, app: str, args: Sequence[str], log: Path) -> StepOutcome:
        """Start the application. `am start -W` stops waiting by itself after about 15 s and prints `Status: timeout`; on a slow
        target that is a start still in progress, not a failure. Then the launch keeps waiting, up to `launchTimeoutSec` counted from
        the start of `am start` (60 s by default, at most 300 s), and decides by evidence: the process is alive, the launched activity
        is resumed and logcat has no crash or ANR of the package. A start error in the `am start` output, a crash or ANR, and a process
        that was running and is gone fail at once with their own reason; a start still not up at the limit fails with a reason of its
        own that says whether the process is alive. The wait is never unbounded."""
        package, activity = split_app(app)
        if not _PACKAGE.fullmatch(package) or (activity is not None and not _ACTIVITY.fullmatch(activity)):
            return StepOutcome(constants.BLOCKED, f"'{app}' is not a package or package/activity")
        self._app = (package, activity or (self._app[1] if self._app and self._app[0] == package else None))
        serial = self._serial(device)
        with self._lock:
            self._closed.discard(serial)
            token = self._launches[serial] = self._launches.get(serial, 0) + 1
        self._diagnostics.pop(serial, None)  # a failure before the folder below is set must not write into an earlier attempt's
        limit = self._launch_timeout
        if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not 1 <= limit <= constants.LAUNCH_TIMEOUT_MAX_SEC:
            return StepOutcome(constants.BLOCKED, f"the launch wait must be 1 to {constants.LAUNCH_TIMEOUT_MAX_SEC} s, not {limit!r}")
        ready = self._ready(device, "launch")
        if not ready.ok:
            return ready
        component, problem = self._resolve_activity(serial, package, self._app[1])
        if component is None:
            return StepOutcome(constants.BLOCKED, problem or "no activity")
        diagnostics = log.parent / DIAGNOSTICS
        diagnostics.mkdir(parents=True, exist_ok=True)
        self._diagnostics[serial] = diagnostics
        self._record_health(device, diagnostics / "health-before.json")
        self._stop_logcat(serial)
        self._adb(serial, "logcat", "-c", timeout=CALL_TIMEOUT_SEC)
        started = self._start_logcat(serial, log, diagnostics / "logcat.log")
        if not started.ok:
            return started
        self._adb(serial, "shell", "input keyevent KEYCODE_WAKEUP", timeout=CALL_TIMEOUT_SEC)
        plan = f"{DATA_ROOT}/{package}/files/gdv/inbox/plan.json"
        began = self._monotonic()
        words = ["am", "start", "-S", "-W", "-n", f"{package}/{component}", "--es", "gdv-plan", plan, *[str(a) for a in args]]
        reply = self._shell(serial, " ".join(quote(word) for word in words), timeout=CALL_TIMEOUT_SEC * 2)
        outcome = self.classify(reply, serial, "am start")
        if not outcome.ok:
            self._stop_own_logcat(serial, token)
            return outcome
        details = {"component": f"{package}/{component}"}
        if re.search(r"^(Error|Exception)\b", reply.output, re.MULTILINE):
            self._stop_own_logcat(serial, token)
            return StepOutcome(constants.FAIL, f"am start did not launch {package}/{component}: {reply.output.strip()[-300:]}")
        if re.search(r"^Status:\s*ok\b", reply.output, re.MULTILINE):
            return StepOutcome(constants.PASS, details=details)
        if not re.search(r"^Status:\s*timeout\b", reply.output, re.MULTILINE):
            self._stop_own_logcat(serial, token)
            return StepOutcome(constants.FAIL, f"am start did not launch {package}/{component}: {reply.output.strip()[-300:]}")
        waited = self._await_start(serial, package, component, reply.output, diagnostics / LAUNCH_NOTES, began, float(limit))
        if not waited.ok:
            self._stop_own_logcat(serial, token)
            return waited
        return StepOutcome(constants.PASS, details={**details, **waited.details})

    def _stop_own_logcat(self, serial: str, token: int) -> None:
        """Stop the capture of a failed launch, unless a newer launch on `serial` has begun: this one may be an abandoned call
        (the pipeline gave up on it) and the capture is by then the newer launch's."""
        with self._lock:
            if self._launches.get(serial) != token:
                return
        self._stop_logcat(serial)

    # -- a slow start ---------------------------------------------------------------------------------------------------------

    def _note(self, path: Path, text: str) -> None:
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"{core.utc_now()} {text}\n")
        except OSError:
            pass  # the notes are evidence for the owner; losing a line must not fail the launch

    def _poll_shell(self, serial: str, script: str) -> supervise.BoundedResult:
        return self._adb(serial, "shell", script, timeout=LAUNCH_POLL_CALL_SEC)

    def _crash_evidence(self, serial: str, package: str) -> tuple[str | None, StepOutcome | None]:
        """A crash or ANR of `package` in the log of this launch (the log was cleared before it), as `(description, problem)`."""
        dump = self._adb(serial, "logcat", "-d", "-v", "threadtime", "AndroidRuntime:E", "ActivityManager:E", "DEBUG:I", "libc:F", "*:S",
                         timeout=LAUNCH_POLL_CALL_SEC)
        if dump.returncode != 0:
            failed = self.classify(dump, serial, "logcat -d")
            return None, (failed if not failed.ok else StepOutcome(constants.FAIL, "logcat -d failed"))
        name = re.escape(package) + r"(?![\w.])"  # not a longer package that starts with this one
        if re.search(rf"FATAL EXCEPTION[^\n]*\n[^\n]*Process: {name}", dump.output):
            return f"FATAL EXCEPTION in {package}", None
        anr = re.search(rf"ANR in {name}[^\n]*", dump.output)
        if anr:
            return anr.group(0).strip()[:160], None
        native = re.search(rf"Fatal signal \d+ [^\n]*\({name}\)|>>> {name} <<<", dump.output)
        if native:
            return f"native crash of {package}: {native.group(0)[:120]}", None
        return None, None

    def _alive(self, serial: str, package: str) -> tuple[bool, StepOutcome | None]:
        reply = self._poll_shell(serial, f"pidof {package}")
        if reply.returncode == 0 and reply.output.strip():
            return True, None
        if reply.returncode in (0, 1) and not reply.output.strip():
            return False, None  # pidof: no such process (an old adb may not carry the exit code, so 0 with no pid is the same)
        failed = self.classify(reply, serial, "pidof")
        return False, (failed if not failed.ok else StepOutcome(constants.FAIL, f"pidof {package} gave an unexpected answer: {reply.output.strip()[:120]}"))

    def _resumed(self, serial: str, package: str, component: str) -> tuple[bool, StepOutcome | None]:
        reply = self._poll_shell(serial, "dumpsys activity activities")
        if reply.returncode != 0:
            failed = self.classify(reply, serial, "dumpsys activity activities")
            return False, (failed if not failed.ok else StepOutcome(constants.FAIL, "dumpsys activity activities failed"))
        names = {component}
        if component.startswith(package + "."):
            names.add(component[len(package):])  # the dump prints a class under the package as `.Name`
        alternatives = "|".join(re.escape(n) for n in sorted(names))
        pattern = rf"[Rr]esumed[A-Za-z]*[:=][^\n]*\b{re.escape(package)}/(?:{alternatives})\b"
        return re.search(pattern, reply.output) is not None, None

    def _look(self, serial: str, package: str, component: str) -> tuple[str | None, bool, bool, StepOutcome | None]:
        """One look at a slow start: `(crash, alive, resumed, problem)`. `problem` is the first adb call that failed (not a crash)."""
        crash, problem = self._crash_evidence(serial, package)
        if problem is not None or crash:
            return crash, False, False, problem
        alive, problem = self._alive(serial, package)
        if problem is not None or not alive:
            return None, alive, False, problem
        resumed, problem = self._resumed(serial, package, component)
        return None, alive, resumed, problem

    def _await_start(self, serial: str, package: str, component: str, first: str, notes: Path, began: float,
                     limit: float) -> StepOutcome:
        """`am start -W` printed `Status: timeout`: wait for the start up to `limit` seconds from `began`, deciding by evidence."""
        wait_time = re.search(r"^WaitTime:\s*(\d+)", first, re.MULTILINE)
        wait_ms = wait_time.group(1) if wait_time else "unknown"
        self._note(notes, f"am start: Status: timeout, WaitTime {wait_ms} ms; waiting up to {limit:g} s for {package}/{component}")
        seen_alive = False
        alive = False
        retried = False
        while True:
            crash, alive_now, resumed, problem = self._look(serial, package, component)
            elapsed = self._monotonic() - began  # after the look: the look took time
            if problem is not None:
                left = limit - elapsed
                if retried or left <= 0:
                    self._note(notes, f"after {elapsed:.0f} s: {problem.reason}")
                    return problem
                retried = True  # one transient adb failure (a hung or timed-out call) is not the end of the wait
                self._note(notes, f"after {elapsed:.0f} s: a look failed ({problem.reason}); trying once more")
                self._sleep(min(LAUNCH_POLL_SEC, left))
                continue
            retried = False
            if crash:
                self._note(notes, f"after {elapsed:.0f} s: {crash}")
                return StepOutcome(constants.FAIL, f"{package} crashed while starting (am start had reported a timeout, WaitTime {wait_ms} ms): {crash}")
            alive = alive_now
            if alive:
                seen_alive = True
            elif seen_alive:
                self._note(notes, f"after {elapsed:.0f} s: process of {package} is gone")
                return StepOutcome(constants.FAIL, f"the process of {package} is gone while it was starting (am start had reported a timeout, WaitTime {wait_ms} ms)")
            self._note(notes, f"after {elapsed:.0f} s: process {'alive' if alive else 'not alive'}, activity {'resumed' if resumed else 'not resumed'}, no crash or ANR")
            if resumed:
                return StepOutcome(constants.PASS, details={"launchWaitSec": f"{elapsed:.0f}", "amStartStatus": "timeout", "amStartWaitTimeMs": wait_ms})
            left = limit - elapsed
            if left <= 0:
                break
            self._sleep(min(LAUNCH_POLL_SEC, left))
        return StepOutcome(constants.FAIL, f"launch timed out after {limit:g} s, process {'alive' if alive else 'not alive'} "
                           f"(the activity {package}/{component} was not resumed; am start WaitTime {wait_ms} ms)")

    def _start_logcat(self, serial: str, console: Path, full: Path) -> StepOutcome:
        command = self.adb_command()
        if command is None:
            return StepOutcome(constants.BLOCKED, "adb is not available")
        session = _LogSession()
        try:
            # Raw Unity lines keep `GDV-RESULT ...` at the start of a line, which the pipeline's terminal-line check reads.
            for path, flags, tags in ((console, ["-v", "raw"], ("Unity:V", "*:S")), (full, ["-v", "threadtime"], UNITY_LOG_TAGS)):
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(path, "ab")
                session.handles.append(handle)
                process, tree = supervise.spawn([*command, "-s", serial, "logcat", *flags, *tags], None, None, handle)
                session.trees.append((process, tree))
        except OSError as error:
            session.stop()
            return StepOutcome(constants.BLOCKED, f"cannot start logcat: {error}")
        with self._lock:
            closed = serial in self._closed
            if not closed:
                self._sessions[serial] = session
        if closed:  # the run was terminated while this (abandoned) launch was still running: nothing may be left capturing
            session.stop()
            return StepOutcome(constants.BLOCKED, "the run was terminated while the application was being launched")
        return StepOutcome(constants.PASS)

    def _stop_logcat(self, serial: str, close: bool = False) -> bool:
        """Stop the capture of `serial`; returns whether a capture process had already ended on its own."""
        with self._lock:
            session = self._sessions.pop(serial, None)
            if close:
                self._closed.add(serial)
        if session is None:
            return False
        ended = session.ended_early()
        session.stop()
        return ended

    def _record_health(self, device: DeviceInfo, path: Path) -> None:
        snapshot = self._snapshot(self._serial(device))
        record = {"capturedUtc": core.utc_now(), "batteryPercent": snapshot["battery"], "thermal": snapshot["thermal"],
                  "freeMb": snapshot["freeMb"], "notes": snapshot["notes"]}
        try:
            core.atomic_write_json(path, record)
        except OSError:
            pass

    def terminate(self, device: DeviceInfo, app: str) -> StepOutcome:
        package, _ = split_app(app)
        if not _PACKAGE.fullmatch(package):
            return StepOutcome(constants.BLOCKED, f"'{app}' is not a package or package/activity")
        serial = self._serial(device)
        try:
            ready = self._ready(device, "terminate")
            if not ready.ok:
                return ready
            diagnostics = self._diagnostics.get(serial)
            notes = []
            if diagnostics is not None:
                alive = self._shell(serial, f"pidof {package}")
                running = alive.ok and alive.returncode == 0 and alive.output.strip() != ""
                notes.append("running" if running else "not running at the end of the run")
                if running:
                    for name, command in (("meminfo", f"dumpsys meminfo {package}"), ("gfxinfo", f"dumpsys gfxinfo {package}")):
                        dump = self._shell(serial, command, timeout=CALL_TIMEOUT_SEC * 2)
                        if dump.ok and dump.returncode == 0:
                            try:
                                core.atomic_write_text(diagnostics / f"{name}.txt", dump.output)
                            except OSError:
                                notes.append(f"{name} could not be saved")
                self._record_health(device, diagnostics / "health-after.json")
            self._sleep(0.5)  # let logcat deliver the last lines before it is stopped
            stopped = self._shell(serial, f"am force-stop {package}")
            outcome = self.classify(stopped, serial, "am force-stop")
            return StepOutcome(outcome.status, outcome.reason, details={"state": "; ".join(notes)}) if outcome.ok else outcome
        finally:
            ended = self._stop_logcat(serial, close=True)  # whatever happens above, no logcat is left running
            diagnostics = self._diagnostics.pop(serial, None)
            if ended and diagnostics is not None:
                try:  # the adb server died or restarted during the run: the console copy stops where the capture stopped
                    core.atomic_write_text(diagnostics / "notes.txt", "logcat ended before the run did; console.log and logcat.log are incomplete\n")
                except OSError:
                    pass

    def screenshot(self, device: DeviceInfo, out: Path) -> StepOutcome:
        serial = self._serial(device)
        ready = self._ready(device, "screenshot")
        if not ready.ok:
            return ready
        remote = f"{REMOTE_SCRATCH}/valtrion-screen-{os.getpid()}.png"
        taken = self._shell(serial, f"screencap -p {remote}")
        outcome = self.classify(taken, serial, "screencap")
        if not outcome.ok:
            return outcome
        out.parent.mkdir(parents=True, exist_ok=True)
        pulled = self._adb(serial, "pull", remote, str(out), timeout=PULL_TIMEOUT_SEC)
        self._shell(serial, f"rm -f {remote}")  # the file is this call's own scratch
        return self.classify(pulled, serial, "adb pull screenshot")


register(PLATFORM, AndroidAdapter)
