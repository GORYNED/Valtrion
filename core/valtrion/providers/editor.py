"""Unity Editor adapter: a plan runs as a Unity Test Framework PlayMode test in a background batch-mode Editor.

The run goes through the project's existing runner (the repository's `tools/validation/run-unity-tests.py`, or the one named by
`editor.runner` in `<project>/validation/device/profiles.json`) restricted to the test assembly that holds the plan test
(`editor.planAssembly`). The plan is staged in a private folder and handed to the test through `GDV_PLAN`, `GDV_OUT_ROOT` and
`GDV_RUN_ID`; the harness writes its heartbeat and result under `GDV_OUT_ROOT/<runId>` like a device player does. When the harness
wrote no result, or the NUnit XML disagrees with it, the adapter normalizes the NUnit XML into the result. The target is always
`evidence: editor`: an Editor number is never a device-performance baseline. A run holds a lease on the project folder and a `unity-editor` slot (of the host's capacity, see `lease.unity_editor_capacity`) from
before the runner starts until no process of its tree is left; a lease that is lost meanwhile is taken again or the Editor is stopped.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .. import constants, core, doctor, lease, supervise, unityguard
from ..adapter import Capabilities, DeviceHealth, DeviceInfo, PlatformAdapter, StepOutcome
from . import register
from .unity import support

LEASE_TTL_SEC = 600
WITHHOLD_RESULT_SEC = 120.0
MAX_LEASE_WAIT_SEC = 240.0  # below the pipeline's bound on one adapter call, so a launch is never abandoned while it still waits
DEFAULT_RUNNER = Path("tools") / "validation" / "run-unity-tests.py"
MAX_STEPS = 200
EDITOR_NOTE = "Editor evidence: not a device-performance baseline"


def find_runner(project: Path, configured: str | None) -> Path | None:
    if configured:
        path = (project / configured).resolve()
        return path if path.is_file() else None
    for parent in [project, *project.parents]:
        candidate = parent / DEFAULT_RUNNER
        if candidate.is_file():
            return candidate
    return None


def normalize_nunit(path: Path) -> tuple[str, str | None, list[dict], dict[str, int]]:
    """(status, reason, steps, counts) of an NUnit XML file: one step per test case, the first failures named in the reason."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as error:
        return constants.FAIL, f"the NUnit result {path.name} is unreadable: {error}", [], {}
    cases = list(root.iter("test-case"))
    counts = {"Passed": 0, "Failed": 0, "Skipped": 0, "Inconclusive": 0}
    steps: list[dict] = []
    failures: list[str] = []
    elapsed = 0
    for case in cases:
        outcome = case.get("result", "")
        counts[outcome] = counts.get(outcome, 0) + 1
        try:
            duration = int(float(case.get("duration", "0") or 0) * 1000)
        except ValueError:
            duration = 0
        status = constants.PASS if outcome == "Passed" else constants.FAIL if outcome in ("Failed", "Inconclusive") else constants.BLOCKED
        name = case.get("fullname") or case.get("name") or "test"
        message = case.findtext("failure/message") or case.findtext("reason/message") or ""
        if status == constants.FAIL:
            failures.append(f"{name}: {message.strip()[:200] or outcome}")
        if len(steps) < MAX_STEPS:
            step = {"type": "test:" + core.safe_identifier(name, "test", 90), "status": status, "startMs": elapsed, "durationMs": duration}
            if message:
                step["message"] = message.strip()[:300]
            steps.append(step)
        elapsed += duration
    if not cases:
        return constants.FAIL, f"{path.name} holds no test cases", steps, counts
    if counts["Passed"] == 0:
        return constants.FAIL, f"no test passed in {path.name}", steps, counts
    if failures:
        return constants.FAIL, f"{len(failures)} test(s) failed: " + "; ".join(failures[:3]), steps, counts
    return constants.PASS, None, steps, counts


class EditorAdapter(PlatformAdapter):
    platform = "editor"
    #: Added to the plan's budget by the pipeline: a batch-mode Editor opens and compiles the project before the plan starts.
    startup_allowance_sec = 900.0
    #: The pipeline takes no unity-editor slot for this platform's build stage (there is nothing to build); launch takes the slot.
    build_uses_editor = False

    def __init__(
            self, hub_environ: Mapping[str, str] | None = None, spawn: Callable | None = None, python: str | None = None,
            home: Path | None = None, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._environ = hub_environ
        self._spawn = spawn or supervise.spawn
        self._python = python or sys.executable
        self._home = home
        self._monotonic = monotonic
        self._project: Path | None = None
        self._profile: str | None = None  # from the build stage; without one the first profile that has an editor entry is used
        self._stage: Path | None = None
        self._process = None
        self._tree = None
        self._log_handle = None
        self._runner_out: Path | None = None
        self._keeper: lease.LeaseKeeper | None = None
        self._leases: lease.LeaseManager | None = None
        self._lease_resource: str | None = None
        self._session = f"editor-{os.getpid()}-{core.sha256_bytes(str(time.time()).encode())[:6]}"
        self._seq = 0
        self._seen_size = 0
        self._started = 0.0
        self._withheld_since: float | None = None
        self._finalized = False
        self._forced_exit: str | None = None
        self._owner = ""
        self._notes: list[str] = []  # what the result must admit about the lease (see `_enforce_leases`)

    def use_project(self, project: Path) -> None:
        """Called by the pipeline with the Unity project folder before any stage runs."""
        self._project = Path(project)

    # -- discovery -------------------------------------------------------------------------------------------------------------

    def _editors(self) -> list[dict]:
        return doctor.find_editors(core.local_os(), dict(self._environ) if self._environ is not None else None) \
            if core.local_os() in ("windows", "macos", "linux") else []

    def probe(self) -> Capabilities:
        if not self._editors():
            return Capabilities(self.platform, (), {"unity": "no Unity editor was found under the Hub paths"})
        return Capabilities(self.platform, ("build", "run", "playmode"))

    def discover(self) -> Sequence[DeviceInfo]:
        editors = self._editors()
        if not editors:
            return []
        versions = ", ".join(e["version"] for e in editors)
        return [DeviceInfo(f"editor:{core.local_host_id()}", self.platform, "unity-editor", constants.EVIDENCE_EDITOR, "Unity Editor",
                           f"Unity {versions}", ("playmode", "editor"), exclusive=False)]

    # -- build and install -----------------------------------------------------------------------------------------------------

    def build(self, project: Path, profile: str, out: Path) -> StepOutcome:
        """There is nothing to build for the Editor: the runner compiles the project when it opens it. This only confirms the
        project and its Editor exist, and returns the project as the 'artifact' so that an install stage has something to name."""
        self._project = Path(project)
        self._profile = profile
        editor, why = support.find_editor(support.project_unity_version(self._project), core.local_os(), self._environ)
        if editor is None:
            return StepOutcome(constants.BLOCKED, why or "no Unity editor")
        return StepOutcome(constants.PASS, artifacts=(self._project,), details={})

    def install(self, device: DeviceInfo, artifact: Path) -> StepOutcome:
        return StepOutcome(constants.PASS)

    # -- files -----------------------------------------------------------------------------------------------------------------

    def _staging(self) -> Path:
        if self._stage is None:
            self._stage = Path(tempfile.mkdtemp(prefix="gdv-editor-"))
            atexit.register(shutil.rmtree, str(self._stage), True)  # a pipeline that stops early never calls terminate
        return self._stage

    def push(self, device: DeviceInfo, local: Path, remote: str) -> StepOutcome:
        relative = support.safe_relative(remote)
        if relative is None:
            return StepOutcome(constants.BLOCKED, f"{remote!r} is not a path inside the Editor run's data folder")
        problem = support.copy_tolerant(Path(local), self._staging() / relative)
        return StepOutcome(constants.PASS) if problem is None else StepOutcome(constants.FAIL, f"push failed: {problem}")

    def pull(self, device: DeviceInfo, remote: str, local: Path) -> StepOutcome:
        relative = support.safe_relative(remote)
        if relative is None:
            return StepOutcome(constants.BLOCKED, f"{remote!r} is not a path inside the Editor run's data folder")
        source = self._staging() / relative
        is_out = relative.name == "out"
        self._enforce_leases()
        alive = self._alive()  # decided once: a runner that ends during the copy is handled by the next pull
        if is_out and self._process is not None and not self._finalized and not alive:
            self._finalize(source)
            self._finalized = True
        if not source.exists():
            local.mkdir(parents=True, exist_ok=True)
        else:
            support.copy_tolerant(source, local)
        if is_out and alive:
            self._while_running(Path(local))
        return StepOutcome(constants.PASS)

    # -- run -------------------------------------------------------------------------------------------------------------------

    def launch(self, device: DeviceInfo, app: str, args: Sequence[str], log: Path) -> StepOutcome:
        project = self._project
        if project is None:
            return StepOutcome(constants.BLOCKED, "the Unity project is not known (pass --project-dir)")
        try:
            profiles = support.load_profiles(project)
        except support.ProfileError as error:
            return StepOutcome(constants.BLOCKED, str(error))
        _, settings = support.platform_profile(profiles, self._profile, "editor")
        settings = settings or {}
        assembly = app if app and app != "editor" else settings.get("planAssembly")
        if not assembly:
            return StepOutcome(constants.BLOCKED, f"{project / support.PROFILES_RELATIVE} names no editor.planAssembly (the test assembly "
                               "that holds the plan test); pass it as --app or add it to the profile")
        runner = find_runner(project, settings.get("runner"))
        if runner is None:
            return StepOutcome(constants.BLOCKED, "no Unity test runner found: add editor.runner to profiles.json or provide "
                               f"{DEFAULT_RUNNER} in the repository")
        editor, why = support.find_editor(support.project_unity_version(project), core.local_os(), self._environ)
        if editor is None:
            return StepOutcome(constants.BLOCKED, why or "no Unity editor")
        if self._alive():
            return StepOutcome(constants.BLOCKED, "an Editor runner of this run is still running; terminate it before launching again")
        stage = self._staging()
        plan = stage / "gdv" / "inbox" / "plan.json"
        if not plan.is_file():
            return StepOutcome(constants.BLOCKED, "the plan was not pushed to the run's inbox before the launch")
        reason = self._acquire_slot(min(float(settings.get("leaseWaitSec", 0)), MAX_LEASE_WAIT_SEC))
        if reason:
            self._discard_stage()
            return StepOutcome(constants.BLOCKED, reason)
        try:
            return self._start_runner(stage, runner, editor, str(assembly), settings, args, log, plan)
        except Exception as error:  # noqa: BLE001 - nothing may leave the lease or the staging folder behind
            if not self._stop_tree():  # a runner that did start keeps the capacity it occupies
                return StepOutcome(constants.FAIL, f"cannot start the Unity runner: {type(error).__name__}: {error}; "
                                   "a started runner is still alive, so its leases stay held until terminate")
            self._release_slot()
            self._close_log()
            self._discard_stage()
            return StepOutcome(constants.FAIL, f"cannot start the Unity runner: {type(error).__name__}: {error}")

    def _discard_stage(self) -> None:
        if self._stage is not None:
            shutil.rmtree(self._stage, ignore_errors=True)
            self._stage = None

    def _start_runner(self, stage: Path, runner: Path, editor: Mapping[str, Any], assembly: str, settings: Mapping[str, Any],
                      args: Sequence[str], log: Path, plan: Path) -> StepOutcome:
        self._runner_out = stage / "runner-out"
        out_root = stage / "gdv" / "out"
        out_root.mkdir(parents=True, exist_ok=True)
        run_id = core.safe_identifier("editor-" + core.sha256_bytes(str(self._monotonic()).encode())[:8])
        argv = [self._python, str(runner), "--editor", editor["path"], "--output", str(self._runner_out), "--assemblies", str(assembly),
                "--timeout", str(int(settings.get("suiteTimeoutSec", 3600))), *[str(a) for a in args]]
        environment = dict(os.environ)
        environment.update({"GDV_PLAN": str(plan), "GDV_OUT_ROOT": str(out_root), "GDV_RUN_ID": run_id, "GDV_EVIDENCE": "editor"})
        log.parent.mkdir(parents=True, exist_ok=True)
        self._close_log()  # the log of an earlier launch that was never terminated
        self._log_handle = open(log, "ab")
        try:
            self._process, self._tree = self._spawn(argv, str(runner.parent), environment, self._log_handle)
        except OSError as error:
            self._release_slot()
            self._close_log()
            self._discard_stage()
            return StepOutcome(constants.BLOCKED, f"cannot start the Unity runner: {error}")
        self._started = self._monotonic()
        self._finalized = False
        self._withheld_since = None
        self._forced_exit = None
        self._seq = self._seen_size = 0
        return StepOutcome(constants.PASS, details={"layer.launch": constants.PASS, "runId": run_id, "assembly": str(assembly)})

    def _acquire_slot(self, wait_sec: float) -> str | None:
        """Take the leases an Editor needs before it starts: the project folder (one Unity process per project), then one slot of the
        host's `unity-editor` capacity (the same total every other claimant counts). All are renewed until `_release_slot`."""
        self._release_slot()  # leases of an earlier launch that was never terminated: never keep two keepers
        project = self._project
        if project is None:
            return "the Unity project is not known (pass --project-dir)"
        manager = lease.LeaseManager(self._home, host_id=core.local_host_id())
        try:
            capacity = lease.unity_editor_capacity(self._home)
        except ValueError as error:
            return str(error)
        owner = os.environ.get("GDV_OWNER") or f"valtrion@{core.local_host_id()}"
        pid = os.getpid()
        self._owner = owner
        deadline = self._monotonic() + max(0.0, wait_sec)
        taken = manager.acquire(lease.unity_project_resource(project), owner, self._session, LEASE_TTL_SEC,
                                f"valtrion editor run of {project.name}", pid, wait_sec=wait_sec)
        if not taken.acquired:
            return taken.reason or "the Unity project is in use by another run"
        slot = manager.acquire_slot(lease.UNITY_EDITOR_RESOURCE, capacity, owner, self._session, LEASE_TTL_SEC,
                                    "valtrion editor run", pid, wait_sec=max(0.0, deadline - self._monotonic()))
        if not slot.acquired:
            manager.release(taken.lease.resource, self._session, pid)
            return slot.reason or "no unity-editor slot is free"
        self._leases = manager
        self._lease_resource = slot.lease.resource
        self._keeper = lease.LeaseKeeper(manager, self._session, pid, LEASE_TTL_SEC / 3, owner)
        self._keeper.add(taken.lease.resource)
        self._keeper.add(slot.lease.resource)
        self._keeper.start()
        # With the leases held (and renewed): no Editor outside a lease and enough memory, within the lease wait (host.json unityGuard).
        reason = unityguard.blocked_reason(manager, self._home, max(0.0, deadline - self._monotonic()))
        if reason:
            self._release_slot()
            return reason
        atexit.register(self._at_exit)  # a pipeline that stops without terminate must not leave an Editor or a lease behind
        return None

    def _release_slot(self) -> None:
        if self._keeper is not None:
            self._keeper.stop()
            self._keeper.release_all()
            self._keeper = None
        self._lease_resource = None
        atexit.unregister(self._at_exit)

    def _at_exit(self) -> None:
        try:
            self.terminate(None, "")  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - interpreter shutdown: nothing can be reported any more
            pass

    def _enforce_leases(self) -> None:
        """While the Editor lives its leases must live. A lease the keeper could no longer renew (taken over or removed by hand) is
        taken again at once; when that is impossible the Editor is stopped, never left running beyond the shared capacity."""
        keeper = self._keeper
        if keeper is None or not keeper.lost or not self._alive():
            return
        manager, owner, pid = self._leases, self._owner, os.getpid()
        lost, failed = list(keeper.lost), []
        for resource in lost:  # only what was lost: the leases still held are never given up and taken again
            if resource.startswith(lease.UNITY_EDITOR_RESOURCE + ":slot"):
                try:
                    result = manager.acquire_slot(lease.UNITY_EDITOR_RESOURCE, lease.unity_editor_capacity(self._home), owner,
                                                  self._session, LEASE_TTL_SEC, "valtrion editor run", pid)
                except ValueError:
                    result = None
            else:
                result = manager.acquire(resource, owner, self._session, LEASE_TTL_SEC, "valtrion editor run", pid)
            if result is not None and result.acquired:
                keeper.replace(resource, result.lease.resource)
                if resource == self._lease_resource:
                    self._lease_resource = result.lease.resource
            else:
                failed.append(resource)
        if not failed:
            self._notes.append(f"the lease(s) {', '.join(lost)} were lost while the Editor ran and taken again; the host's Unity limit "
                               "may have been exceeded in between")
            return
        self._forced_exit = (f"the lease(s) {', '.join(failed)} were lost while the Editor ran and could not be taken again; "
                             "the Editor was stopped")
        if self._stop_tree():
            self._release_slot()
        # A tree that survives keeps the keeper and the exit hook: its remaining leases stay held until `terminate` succeeds.

    def _close_log(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            finally:
                self._log_handle = None

    def _alive(self) -> bool:
        return self._process is not None and (self._process.poll() is None or (self._tree is not None and self._tree.alive()))

    # -- settling what the pipeline sees --------------------------------------------------------------------------------------

    def _while_running(self, local: Path) -> None:
        """Make the pulled output honest while the runner is alive: show startup progress before the harness writes anything, and
        hold a harness result back (for a bounded time) until the runner has finished, so the NUnit XML can overrule it."""
        harness_folders = sorted((p for p in local.glob("*") if p.is_dir() and p.name != "adapter"), key=lambda p: p.stat().st_mtime)
        self._heartbeat_while_starting(local, harness_folders)
        if harness_folders and (harness_folders[-1] / "result.json").exists():
            now = self._monotonic()
            self._withheld_since = self._withheld_since if self._withheld_since is not None else now
            if now - self._withheld_since >= WITHHOLD_RESULT_SEC and self._tree is not None and self._forced_exit is None:
                # The plan ended but the runner never confirmed it (a Unity shutdown that hangs): stop it, and the run is not a pass.
                self._forced_exit = (f"the Editor runner did not exit within {WITHHOLD_RESULT_SEC:g} s after the plan finished; "
                                     "it was stopped, so the NUnit result is missing")
                self._tree.kill()
            (harness_folders[-1] / "result.json").unlink()  # the settled result is shown once the runner has ended

    def _heartbeat_while_starting(self, local: Path, harness_folders: list[Path]) -> None:
        pending = local / "adapter"
        if harness_folders:
            shutil.rmtree(pending, ignore_errors=True)
            return
        size = supervise._path_size(self._runner_out) if self._runner_out else 0  # noqa: SLF001 - same package
        log_size = supervise._path_size(Path(self._log_handle.name)) if self._log_handle else 0  # noqa: SLF001
        total = size + log_size
        if total > self._seen_size:
            self._seq += 1
            self._seen_size = total
        pending.mkdir(parents=True, exist_ok=True)
        core.atomic_write_json(pending / "heartbeat.json", {
            "schema": "gdv.heartbeat/1", "runId": "editor-startup", "seq": self._seq, "utc": core.utc_now(), "state": "running",
            "stepIndex": 0, "stepType": "editor.startup", "elapsedMs": int((self._monotonic() - self._started) * 1000),
            "message": "the Editor and its runner are starting; no harness heartbeat yet"})

    def _finalize(self, out_root: Path) -> None:
        """The runner ended. Settle the staged output once. A harness PASS stands only when the runner exited 0 without a failing
        `GDV-RESULT`/`STATUS` line and the NUnit XML confirms it; anything else downgrades it. A run with no harness result gets
        one from the NUnit XML, the runner's own status lines or the exit code, and is never a PASS."""
        code = self._process.poll() if self._process else None
        xml = (self._runner_out / "PlayMode.xml") if self._runner_out else None
        have_xml = xml is not None and xml.is_file()
        nunit_status, nunit_reason, steps, _ = normalize_nunit(xml) if have_xml else (constants.FAIL, "no NUnit result was written", [], {})
        log = Path(self._log_handle.name) if self._log_handle else None
        marker = supervise.read_marker(log) if log else None
        tool = support.read_tool_status(log) if log else None
        runner_status, runner_reason = support.exit_status(code, marker, tool)
        clean_exit = code == 0 and not (marker and marker[0] != constants.PASS) and not (tool and tool[0] != constants.PASS)
        folder = support.latest_run_folder(out_root)
        existing = support_json(folder / "result.json") if folder is not None else None
        if existing is not None and isinstance(existing.get("status"), str):
            status, reason = existing["status"], existing.get("reason")
            if status == constants.PASS:
                if self._forced_exit:
                    status, reason = constants.FAIL, self._forced_exit
                elif not clean_exit:
                    status, reason = runner_status, f"the harness reported PASS but the runner did not finish cleanly: {runner_reason}"
                elif nunit_status != constants.PASS:
                    status, reason = constants.FAIL, f"the harness reported PASS but the NUnit result says: {nunit_reason}"
            existing["status"], existing["reason"] = status, None if status == constants.PASS else (reason or f"the run ended {status}")
            existing["unverified"] = [*existing.get("unverified", []), EDITOR_NOTE, *self._notes]
            core.atomic_write_json(folder / "result.json", existing)
            return
        if self._forced_exit:
            status, reason = constants.FAIL, self._forced_exit
        elif have_xml and nunit_status == constants.PASS and clean_exit:
            # The PlayMode tests passed but the harness left no plan result: that is not a pass of the plan.
            status, reason = constants.FAIL, "the PlayMode tests passed but the harness wrote no plan result"
        elif have_xml and nunit_status != constants.PASS:
            status, reason = nunit_status, nunit_reason
        else:
            status, reason = runner_status, runner_reason
        support.write_exit_result(out_root, status, reason or "", steps, [EDITOR_NOTE, *self._notes])

    def _stop_tree(self) -> bool:
        """Kill the runner's process tree; True when nothing of it is left. A tree that cannot be stopped is kept, with its leases."""
        tree = self._tree
        if tree is None:
            return True
        for _ in range(2):
            try:
                tree.kill()
                if self._process is not None:
                    try:
                        self._process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
                if not tree.alive():
                    break
            except Exception:  # noqa: BLE001 - whether it is dead is what matters, not why the kill call failed
                continue
        try:
            alive = tree.alive()
        except Exception:  # noqa: BLE001
            alive = True
        if alive:
            return False
        tree.close()
        self._tree = None
        return True

    def terminate(self, device: DeviceInfo, app: str) -> StepOutcome:
        """Stop the Editor and give back its leases. The slot and the project lease are released only once no Unity process of this
        run is left: while one lives it still occupies the capacity, so they stay held (and renewed) and a later `terminate` retries."""
        stopped = self._stop_tree()
        self._close_log()
        if not stopped:
            return StepOutcome(constants.FAIL, "Unity processes are still alive after the process tree was killed; "
                               "the unity-editor slot stays held until they are gone")
        self._release_slot()
        self._discard_stage()
        self._process, self._runner_out = None, None
        self._finalized, self._forced_exit, self._withheld_since = False, None, None
        self._notes = []
        return StepOutcome(constants.PASS)

    def screenshot(self, device: DeviceInfo, out: Path) -> StepOutcome:
        return StepOutcome(constants.BLOCKED, "the Editor runs in batch mode; it has no screen to capture (the harness may write screenshots)")

    def health(self, device: DeviceInfo) -> DeviceHealth:
        probe = Path(tempfile.gettempdir())
        free = int(shutil.disk_usage(probe).free // (1024 * 1024))
        return DeviceHealth(True, locked=False, free_space_mb=free, notes=())


def support_json(path: Path) -> dict | None:
    try:
        document = core.read_json(path, attempts=2)
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


register("editor", EditorAdapter)
