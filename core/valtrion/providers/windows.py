"""Windows player adapter: build a standalone player in batch mode, run it under a Job Object, read its data folder.

The player is built with a validation product identity (its own `companyName` and `productName`), so its `persistentDataPath`
(`%USERPROFILE%\\AppData\\LocalLow\\<company>\\<product>`) is not the developer's own. The adapter refuses an identity equal to the
project's, and refuses an artifact whose `app.info` does not carry the expected identity. Plans go to `<data>/gdv/inbox`, results
come back from `<data>/gdv/out`. The player starts under a Job Object (the supervisor's `spawn`), so terminating it, or the valtrion job
dying, ends the whole tree. Windows is a `host` evidence class: its numbers are never device-performance evidence.
"""

from __future__ import annotations

import os
import platform as host_platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .. import config, constants, core, doctor, retention, supervise
from ..adapter import Capabilities, DeviceHealth, DeviceInfo, PlatformAdapter, StepOutcome
from . import register
from .unity import support

DEFAULT_COMPANY = "Valtrion Validation"
BUILD_TIMEOUT_SEC = 3600.0
BUILD_STALL_SEC = 600.0
TERMINATE_WAIT_SEC = 10.0
_SAFE_FILE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_SCREENSHOT_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
    "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
    "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
    "$g=[System.Drawing.Graphics]::FromImage($bmp);$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
    "$bmp.Save($args[0],[System.Drawing.Imaging.ImageFormat]::Png)")


def default_local_low() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "AppData" / "LocalLow"


class WindowsAdapter(PlatformAdapter):
    platform = "windows"

    def __init__(
            self, local_low: Path | None = None, hub_environ: Mapping[str, str] | None = None, spawn: Callable | None = None,
            is_windows: bool | None = None, run: Callable[..., Any] | None = None,
            desktop: Callable[[], bool | None] = support.interactive_desktop) -> None:
        self._desktop = desktop
        self._local_low = local_low or default_local_low()
        self._environ = hub_environ
        self._spawn = spawn or supervise.spawn
        self._is_windows = support.is_host_windows() if is_windows is None else is_windows
        self._run = run or supervise.run_bounded
        self._exe: Path | None = None
        self._project: Path | None = None
        self._identity: tuple[str, str] | None = None
        #: Files this run pushed into the data root outside the harness folder (pre-launch files); they belong to the run too.
        self._pushed_outside: list[Path] = []
        self._process = None
        self._tree = None
        self._log_handle = None
        self._console: Path | None = None
        self._exit_written = False
        self._last_screen: Path | None = None
        self._launched_at = 0.0

    def use_project(self, project: Path) -> None:
        """Called by the pipeline with the Unity project folder; `install` compares the player's identity with the project's own."""
        self._project = Path(project)

    # -- discovery -------------------------------------------------------------------------------------------------------------

    def probe(self) -> Capabilities:
        if not self._is_windows:
            return Capabilities(self.platform, (), {"windows": f"the Windows player runs on a Windows host, not on {core.local_os()}"})
        editors = doctor.find_editors("windows", dict(self._environ) if self._environ is not None else None)
        blocked: dict[str, str] = {}
        if not editors:
            blocked["build"] = "no Unity editor was found under the Hub paths"
        if self._desktop() is False:
            blocked["desktop"] = "no interactive desktop in this session (SSH or service): builds work, rendering player runs are BLOCKED"
        available = ["run"] + ([] if "build" in blocked else ["build"])
        return Capabilities(self.platform, tuple(available), blocked)

    def discover(self) -> Sequence[DeviceInfo]:
        if not self._is_windows:
            return []
        version = f"Windows {host_platform.release()} ({host_platform.version()})"
        return [DeviceInfo(
            f"windows:{core.local_host_id()}", self.platform, "windows-desktop", constants.EVIDENCE_HOST, host_platform.node(), version,
            ("desktop", "mouse", "keyboard"), exclusive=False)]

    # -- build and install -----------------------------------------------------------------------------------------------------

    def _profile(self, project: Path, profile: str) -> tuple[dict | None, str | None]:
        try:
            profiles = support.load_profiles(project)
        except support.ProfileError as error:
            return None, str(error)
        _, entry = support.platform_profile(profiles, profile, "windows")
        if entry is None or not entry.get("executeMethod"):
            return None, (f"{project / support.PROFILES_RELATIVE} names no profiles.{profile}.windows.executeMethod: the project must provide a "
                          "build hook that honors -buildOutput, -gdvProductName and -gdvCompanyName")
        return entry, None

    def identity_for(self, project: Path, entry: Mapping[str, Any]) -> tuple[str, str]:
        company = str(entry.get("companyName") or DEFAULT_COMPANY)
        product = str(entry.get("productName") or f"{project.name} Validation")
        return company, product

    def data_root(self, company: str, product: str) -> Path:
        return self._local_low / company / product

    def build(self, project: Path, profile: str, out: Path) -> StepOutcome:
        if not self._is_windows:
            return StepOutcome(constants.BLOCKED, f"a Windows player is built on a Windows host, not on {core.local_os()}")
        entry, why = self._profile(project, profile)
        if entry is None:
            return StepOutcome(constants.BLOCKED, why or "no build profile")
        company, product = self.identity_for(project, entry)
        own_company, own_product = support.project_identity(project)
        if (company, product) == (own_company, own_product):
            return StepOutcome(constants.BLOCKED, f"the validation identity {company!r}/{product!r} is the project's own, so the player "
                               "would write into the developer's data; name a separate productName in the profile")
        editor, why = support.find_editor(support.project_unity_version(project), "windows", self._environ)
        if editor is None:
            return StepOutcome(constants.BLOCKED, why or "no Unity editor")
        if support.editor_module_folder(editor, "windowsstandalonesupport") is None:
            return StepOutcome(constants.BLOCKED, "the Unity Windows Build Support module is not installed; add it in Unity Hub (owner action)",
                               details={"layer.export": constants.BLOCKED})
        backend = str(entry.get("scriptingBackend", "mono")).lower()
        if backend == "il2cpp" and not il2cpp_available(editor):
            return StepOutcome(
                constants.BLOCKED, "Windows Build Support (IL2CPP) is not installed for this Editor; add the module in Unity Hub (owner action)",
                details={"layer.export": constants.BLOCKED})
        out.mkdir(parents=True, exist_ok=True)
        exe_name = _SAFE_FILE.sub("-", product) + ".exe"
        exe = out / exe_name
        log = out.parent / "logs" / "unity-build.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        argv = [editor["path"], "-batchmode", "-quit", "-projectPath", str(project), *config.unity_accelerator_args(), "-buildTarget", "Win64",
                "-executeMethod", str(entry["executeMethod"]), "-buildOutput", str(exe), "-gdvProductName", product,
                "-gdvCompanyName", company, "-gdvBackend", backend, "-gdvDevelopment", "-logFile", str(log), *[str(a) for a in entry.get("extraArgs", [])]]
        outcome = support.run_unity(argv, log, float(entry.get("timeoutSec", BUILD_TIMEOUT_SEC)), float(entry.get("stallSec", BUILD_STALL_SEC)))
        if outcome.status != constants.PASS or not exe.is_file():
            status = outcome.status if outcome.status != constants.PASS else constants.FAIL
            why = outcome.reason or "the build hook finished but produced no player executable"
            layer = "compile" if support.compile_errors(log) else "nativeBuild"
            return StepOutcome(status, f"the Windows build did not succeed: {why}", (log,), {f"layer.{layer}": constants.FAIL})
        verdict = self._check_identity(exe, company, product)
        if verdict is not None:
            return StepOutcome(constants.FAIL, verdict, (log,), {"layer.nativeBuild": constants.FAIL})
        return StepOutcome(constants.PASS, artifacts=(exe,), details={
            "layer.compile": constants.PASS, "layer.export": constants.PASS, "layer.nativeBuild": constants.PASS})

    @staticmethod
    def read_app_info(exe: Path) -> tuple[str, str] | None:
        """(company, product) the player was built with: the first two lines of `<name>_Data/app.info`."""
        try:
            lines = (exe.parent / f"{exe.stem}_Data" / "app.info").read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        return (lines[0].strip(), lines[1].strip()) if len(lines) >= 2 else None

    def _check_identity(self, exe: Path, company: str, product: str) -> str | None:
        found = self.read_app_info(exe)
        if found is None:
            return f"the player has no readable {exe.stem}_Data/app.info, so its data folder cannot be verified as isolated"
        if found != (company, product):
            return (f"the player was built as {found[0]!r}/{found[1]!r}, not the validation identity {company!r}/{product!r}; "
                    "the build hook did not apply -gdvProductName/-gdvCompanyName")
        return None

    def _no_desktop(self) -> StepOutcome | None:
        if self._desktop() is False:
            return StepOutcome(constants.BLOCKED, "this session has no interactive desktop (an SSH session or a service): a rendering player "
                               "cannot draw there and would never report; run the job from a logged-in desktop session (owner action)")
        return None

    def install(self, device: DeviceInfo, artifact: Path) -> StepOutcome:
        blocked = self._no_desktop()
        if blocked is not None:
            return blocked
        exe = Path(artifact)
        if not exe.is_file() or exe.suffix.lower() != ".exe":
            return StepOutcome(constants.BLOCKED, f"{exe} is not a Windows player executable")
        found = self.read_app_info(exe)
        if found is None:
            return StepOutcome(constants.BLOCKED, f"{exe.stem}_Data/app.info is missing; cannot tell which data folder the player uses")
        company, product = found
        if self._project is not None and (company, product) == support.project_identity(self._project):
            return StepOutcome(constants.BLOCKED, f"the player was built under the project's own identity {company!r}/{product!r}; installing it "
                               "would clear and write the developer's data folder")
        root = self.data_root(company, product)
        if root.parent.parent != self._local_low or ".." in (company, product):
            return StepOutcome(constants.BLOCKED, f"refusing the data root {root}: it must be <company>/<product> under {self._local_low}")
        self._exe, self._identity = exe, (company, product)
        self._pushed_outside = []
        # The validation identity owns this folder alone, so a fresh run starts from an empty harness area. Data still there belongs to
        # a run that did not clean up (a passing run deletes its data), so it is kept aside as diagnostics, not cleared by this run.
        area = root / "gdv"
        if area.exists():
            try:
                if area.is_dir() and any(area.iterdir()):
                    retention.retain_folder(area, "INTERRUPTED", "an earlier run's data was still here when the next install started")
                else:
                    shutil.rmtree(area, ignore_errors=True)
            except OSError as error:
                return StepOutcome(constants.FAIL, f"cannot keep the earlier harness data {area}: {error}")
        return StepOutcome(constants.PASS, details={"layer.install": constants.PASS, "dataRoot": str(root)})

    def cleanup_run_data(self, device: DeviceInfo) -> StepOutcome:
        """After a passing run and once its output was collected: removes the harness area of the validation identity."""
        root = self._data()
        if root is None:
            return StepOutcome(constants.PASS)
        if self._process is not None and self._process.poll() is None and (self._pushed_outside or (root / "gdv").exists()):
            return StepOutcome(constants.BLOCKED, "the player is still running, so its data folder is left in place")
        stuck = []
        for relative in self._pushed_outside:  # pre-launch files: a later run on this identity must not start from them
            try:
                (root / relative).unlink(missing_ok=True)
            except OSError:
                stuck.append(str(root / relative))
        if stuck:
            return StepOutcome(constants.BLOCKED, "pre-launch files could not be removed (a file is locked): " + ", ".join(stuck))
        self._pushed_outside.clear()
        if not (root / "gdv").exists():
            return StepOutcome(constants.PASS)
        shutil.rmtree(root / "gdv", ignore_errors=True)
        if (root / "gdv").exists():
            return StepOutcome(constants.BLOCKED, f"{root / 'gdv'} could not be removed (a file is locked)")
        return StepOutcome(constants.PASS)

    def retain_run_data(self, device: DeviceInfo, status: str, reason: str | None) -> StepOutcome:
        """After a failed or interrupted run: keeps the harness area as diagnostics, with `retained.json`, moved aside as
        `gdv.retained-<utc>`. No later install clears it; the task that started the run removes it after review."""
        root = self._data()
        if root is None:
            return StepOutcome(constants.PASS)
        running = self._process is not None and self._process.poll() is None
        if self._pushed_outside and running:
            return StepOutcome(constants.BLOCKED, "the player is still running, so its pre-launch files are left in place: "
                               + ", ".join(str(root / r) for r in self._pushed_outside))
        if self._pushed_outside:
            # Pre-launch files move into the harness area as `prelaunch/<path>` so they are kept with the rest of the run's data.
            for relative in self._pushed_outside:
                source = root / relative
                if source.is_file():
                    try:
                        (root / "gdv" / "prelaunch" / relative).parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(source), str(root / "gdv" / "prelaunch" / relative))
                    except OSError as error:
                        return StepOutcome(constants.FAIL, f"cannot keep the pre-launch file {source}: {error}")
            self._pushed_outside.clear()
        if not retention.has_content(root / "gdv"):
            return StepOutcome(constants.PASS)  # the run left nothing worth keeping
        if self._process is not None and self._process.poll() is None:
            return StepOutcome(constants.BLOCKED, "the player is still running, so its data folder is left in place")
        try:
            target = retention.retain_folder(root / "gdv", status, reason)
        except OSError as error:
            return StepOutcome(constants.FAIL, f"cannot keep the harness data {root / 'gdv'}: {error}")
        return StepOutcome(constants.PASS, details={"retainedAt": str(target)})

    # -- files -----------------------------------------------------------------------------------------------------------------

    def _data(self) -> Path | None:
        return self.data_root(*self._identity) if self._identity else None

    def push(self, device: DeviceInfo, local: Path, remote: str) -> StepOutcome:
        root = self._data()
        relative = support.safe_relative(remote)
        if root is None:
            return StepOutcome(constants.BLOCKED, "nothing is installed: the data folder of the player is not known")
        if relative is None:
            return StepOutcome(constants.BLOCKED, f"{remote!r} is not a path inside the player's data folder")
        if relative.parts[0].casefold() != "gdv" and relative not in self._pushed_outside:
            self._pushed_outside.append(relative)  # recorded before the copy, so a partial copy is cleaned up or kept like a whole one
        problem = support.copy_tolerant(Path(local), root / relative)
        return StepOutcome(constants.PASS) if problem is None else StepOutcome(constants.FAIL, f"push failed: {problem}")

    def pull(self, device: DeviceInfo, remote: str, local: Path) -> StepOutcome:
        root = self._data()
        relative = support.safe_relative(remote)
        if root is None:
            return StepOutcome(constants.BLOCKED, "nothing is installed: the data folder of the player is not known")
        if relative is None:
            return StepOutcome(constants.BLOCKED, f"{remote!r} is not a path inside the player's data folder")
        source = root / relative
        # Decided before the copy: a player that finishes and exits during the copy is read by the next pull, never mistaken for a
        # player that died without a result.
        exited_before = self._process is not None and self._process.poll() is not None and not (self._tree is not None and self._tree.alive())
        if not source.exists():
            if relative.suffix:
                return StepOutcome(constants.BLOCKED, f"{source} does not exist")
            local.mkdir(parents=True, exist_ok=True)  # no output folder yet is normal before the first launch
        else:
            problem = support.copy_tolerant(source, local)
            if problem is not None and "locked" not in problem:
                return StepOutcome(constants.FAIL, f"pull failed: {problem}")
        if exited_before:
            self._write_exit_result_if_needed(Path(local), remote)
        return StepOutcome(constants.PASS)

    def _write_exit_result_if_needed(self, local: Path, remote: str) -> None:
        """A player that is gone without a result is a finished run, not a hang: say so now instead of after the stall window."""
        if self._process is None or self._exit_written or remote.rstrip("/\\").split("/")[-1] != "out":
            return
        code = self._process.poll()
        if code is None or self._tree is not None and self._tree.alive():
            return
        folder = support.latest_run_folder(local)
        if folder is not None and (folder / "result.json").exists():
            return
        marker = supervise.read_marker(self._log) if self._log else None
        status, reason = support.exit_status(code, marker)
        support.write_exit_result(local, status, reason or "", unverified=("the player ended without writing a result",))
        self._exit_written = True

    # -- run -------------------------------------------------------------------------------------------------------------------

    _log: Path | None = None

    def launch(self, device: DeviceInfo, app: str, args: Sequence[str], log: Path) -> StepOutcome:
        blocked = self._no_desktop()
        if blocked is not None:
            return blocked
        exe = Path(app) if app and Path(app).is_absolute() and Path(app).is_file() else self._exe
        if exe is None or not exe.is_file():
            return StepOutcome(constants.BLOCKED, "no installed player: run the install stage (or pass the player's absolute path as the app)")
        if self._process is not None and self._process.poll() is None:
            return StepOutcome(constants.BLOCKED, "a player of this run is already running; terminate it first")
        root = self._data()
        plan = root / "gdv" / "inbox" / "plan.json" if root else None
        log.parent.mkdir(parents=True, exist_ok=True)
        stdout = log.with_name(log.name + ".stdout")
        argv = [str(exe)]
        if plan is not None and plan.is_file():
            argv += ["-gdv-plan", str(plan)]
        argv += ["-logFile", str(log), "-screen-fullscreen", "0", "-screen-width", "1280", "-screen-height", "720", *[str(a) for a in args]]
        self._log = log
        self._log_handle = open(stdout, "ab")
        if self._tree is not None:
            self._tree.close()
            self._tree = None
        try:
            self._process, self._tree = self._spawn(argv, str(exe.parent), None, self._log_handle)
        except OSError as error:
            self._close_log()
            return StepOutcome(constants.BLOCKED, f"cannot start {exe}: {error}")
        self._exit_written = False
        self._last_screen = None  # a picture of an earlier run is never this run's failure screenshot
        return StepOutcome(constants.PASS, details={"layer.launch": constants.PASS, "pid": str(self._process.pid)})

    def _close_log(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            finally:
                self._log_handle = None

    def terminate(self, device: DeviceInfo, app: str) -> StepOutcome:
        if self._process is None or self._tree is None:
            return StepOutcome(constants.PASS)
        if self._process.poll() is None or self._tree.alive():
            self._capture_before_kill()
        try:
            self._tree.kill()
            try:
                self._process.wait(timeout=TERMINATE_WAIT_SEC)
            except subprocess.TimeoutExpired:
                pass
            alive = self._tree.alive()
        finally:
            self._tree.close()
            self._close_log()
        self._tree = None
        if alive:
            return StepOutcome(constants.FAIL, "processes of the player are still alive after the Job Object was terminated")
        return StepOutcome(constants.PASS)

    def _capture_before_kill(self) -> None:
        """The screen as the hung or finished player left it, taken before the process is killed (the pipeline asks for its
        failure screenshot after terminating). Best effort: a locked or missing desktop session just yields no picture."""
        if self._log is None:
            return
        target = self._log.parent / "screenshots" / "before-terminate.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        result = self._run(["powershell", "-NoProfile", "-NonInteractive", "-Command", f"& {{ {_SCREENSHOT_SCRIPT} }}", str(target)], 30)
        if result.ok and target.is_file() and target.stat().st_size > 0:
            self._last_screen = target

    def screenshot(self, device: DeviceInfo, out: Path) -> StepOutcome:
        out.parent.mkdir(parents=True, exist_ok=True)
        root = self._data()
        if self._last_screen is not None and self._last_screen.is_file():
            shutil.copyfile(self._last_screen, out)
            return StepOutcome(constants.PASS, (out,), {"source": "screen capture before terminate"})
        shots = sorted(root.glob("gdv/out/*/screenshots/*.png"), key=lambda p: p.stat().st_mtime) if root and root.is_dir() else []
        if shots:
            shutil.copyfile(shots[-1], out)
            return StepOutcome(constants.PASS, (out,), {"source": "harness screenshot"})
        return StepOutcome(constants.BLOCKED, "the screen could not be captured and the harness wrote no screenshot")

    def health(self, device: DeviceInfo) -> DeviceHealth:
        if not self._is_windows:
            return DeviceHealth(False, notes=("not a Windows host",))
        root = self._data() or self._local_low
        probe = root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        free = int(shutil.disk_usage(probe).free // (1024 * 1024))
        return DeviceHealth(True, locked=None, free_space_mb=free, notes=("lock state and thermal state are not reported on Windows",))


def il2cpp_available(editor: Mapping[str, Any]) -> bool:
    folder = support.editor_module_folder(editor, "windowsstandalonesupport")
    return folder is not None and (folder / "Variations" / "win64_player_development_il2cpp").is_dir()


register("windows", WindowsAdapter)
