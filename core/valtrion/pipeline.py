"""Pipeline orchestration: build, install, run series, collect, compare, calling `PlatformAdapter` methods.

The pipeline runs inside a job on the host that owns the resources. It selects a device from the adapter's own discovery, takes
the leases each stage needs and only for that stage (`unity-project` and a `unity-editor` slot for the build, released right after
it; `device:<id>` from the first install/run to the last install/run/collect), and treats every stage as a bounded step. A job
never waits for one kind of resource while holding the other, so no wait cycle exists and a build overlaps another job's device run.
A build is published outside the work folder and, for a clean tree, reused by key (see `buildcache`). A
failed build is never installed. A run on the device is watched through the `gdv.heartbeat/1` file the harness writes: its `seq`
must advance, otherwise the run is a STALL; the hard budget of the plan is the TIMEOUT. The result is a `gdv.result/1` document that is
validated before it is written. Everything the run registered as temporary is removed afterwards (see `artifacts`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import artifacts, buildcache, checkout, compare, config, constants, core, diskspace, gitstate, incremental, lease, providers, supervise
from .adapter import DeviceHealth, DeviceInfo, PlatformAdapter, StepOutcome
from .jobs import JobContext
from .jobs import reap_cancelled_jobs as jobs_reap
from .jobs import work_cleanup_blocker as jobs_blocker
from .schema import Validator, validate_document

DEVICE_OUT = "gdv/out"
DEVICE_INBOX = "gdv/inbox/plan.json"
DEFAULT_DEVICE_POLL_SEC = 3.0
#: A build stage without its own timeout: a Unity export plus a native build is long, and an adapter ends its own build inside it.
BUILD_STAGE_DEFAULT_SEC = 14400.0
#: Stages that take the device lease, and the stages the device lease spans (up to the last of them it is held, then released).
DEVICE_CLAIM_STAGES = ("install", "run")
DEVICE_STAGES = ("install", "run", "collect")
_PERFORMANCE_EVIDENCE = (constants.EVIDENCE_PHYSICAL,)
#: Capability an adapter gives a device that is attached but waits for an owner action (see `select_device`).
NEEDS_PERMISSION_CAPABILITY = "needs-permission"
_UNITY_VERSION = re.compile(r"m_EditorVersion:\s*(\S+)")


@dataclass
class PipelineOutcome:
    status: str
    reason: str | None
    cancelled: bool = False
    results: list[dict] = field(default_factory=list)


@dataclass
class PipelineOptions:
    project_dir: Path | None = None
    pipeline_dir: Path | None = None  # plans and the baselines folder resolve against it
    device_poll_sec: float = DEFAULT_DEVICE_POLL_SEC
    op_timeout_sec: float = 300.0
    sha_pinned: bool = False  # `valtrion pipeline --sha`: the commit was named on the command line and must be the one checked out
    checkout_id: str | None = None  # set for a managed checkout (`--checkout`): valtrion owns the folder and checks the commit out in it
    checkout_root: Path | None = None  # the root of that git checkout (the project folder may be a subfolder of it)
    checkout_remote: str | None = None  # what a missing managed checkout is cloned from
    unity_path: str = "."  # the Unity project below the checkout root
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    adapter_factory: Callable[[str], PlatformAdapter | None] = providers.get
    git: compare.GitRunner = field(default_factory=lambda: compare.run_git)


class GuardedAdapter:
    """Bounds every adapter call the pipeline makes, so a hung `devicectl copy` or `adb` cannot hang the run. A call that
    exceeds `op_timeout` or raises becomes a TIMEOUT or FAIL outcome (an unreachable device for `health`). `build` and `install`
    are bounded separately by the stage timeout. Adapters should still use `supervise.run_bounded` for their external tools."""

    def __init__(self, adapter: PlatformAdapter, op_timeout: float = 300.0) -> None:
        self._adapter = adapter
        self._timeout = op_timeout
        self.platform = adapter.platform

    def bind_application(self, app: str) -> None:
        self._adapter.bind_application(app)

    def bind_device(self, device: DeviceInfo) -> None:
        self._adapter.bind_device(device)

    def bind_temporary_registrar(self, register: Callable[[Path], None]) -> None:
        self._adapter.bind_temporary_registrar(register)

    def bind_build_budget(self, seconds: float) -> None:
        self._adapter.bind_build_budget(seconds)

    def _call(self, name: str, *args: Any) -> tuple[bool, Any, str | None, str]:
        try:
            finished, value = supervise.call_bounded(lambda: getattr(self._adapter, name)(*args), self._timeout)
        except Exception as error:  # noqa: BLE001 - adapter code; surfaced as a failure with its type
            return False, None, f"{name} raised {type(error).__name__}: {error}", constants.FAIL
        if not finished:
            return False, None, f"{name} did not return within {self._timeout:g} s", constants.TIMEOUT
        return True, value, None, constants.PASS

    def _outcome(self, name: str, *args: Any) -> StepOutcome:
        ok, value, reason, status = self._call(name, *args)
        return value if ok else StepOutcome(status, reason or name)

    @property
    def build_uses_editor(self) -> bool:
        return bool(getattr(self._adapter, "build_uses_editor", True))

    @property
    def startup_allowance_sec(self) -> float:
        return float(getattr(self._adapter, "startup_allowance_sec", 0.0))

    @property
    def signs_for_device(self) -> bool:
        """Whether the build is signed for one device, which then belongs to the build cache key."""
        return bool(getattr(self._adapter, "signs_for_device", False))

    def toolchain(self) -> Mapping[str, str]:
        """Optional adapter hook: the tool versions that change a build (for example the Xcode version), part of the cache key."""
        hook = getattr(self._adapter, "toolchain", None)
        return dict(hook()) if hook is not None else {}

    def discover(self) -> Sequence[DeviceInfo]:
        ok, value, reason, _ = self._call("discover")
        if not ok:
            raise RuntimeError(reason)
        return value

    def health(self, device: DeviceInfo):
        ok, value, reason, _ = self._call("health", device)
        return value if ok else DeviceHealth(False, notes=(reason or "health failed",))

    def build(self, *args: Any) -> StepOutcome:
        return self._adapter.build(*args)

    def install(self, *args: Any) -> StepOutcome:
        return self._adapter.install(*args)

    def prepare(self, *args: Any) -> StepOutcome:
        return self._outcome("prepare", *args)

    def push(self, *args: Any) -> StepOutcome:
        return self._outcome("push", *args)

    def pull(self, *args: Any) -> StepOutcome:
        return self._outcome("pull", *args)

    def launch(self, *args: Any) -> StepOutcome:
        return self._outcome("launch", *args)

    def terminate(self, *args: Any) -> StepOutcome:
        return self._outcome("terminate", *args)

    def screenshot(self, *args: Any) -> StepOutcome:
        return self._outcome("screenshot", *args)

    def cleanup_run_data(self, device: DeviceInfo) -> StepOutcome:
        """Optional adapter hook: remove what the run left in an isolated data folder, after its output was collected."""
        if not hasattr(self._adapter, "cleanup_run_data"):
            return StepOutcome(constants.PASS)
        return self._outcome("cleanup_run_data", device)

    def retain_run_data(self, device: DeviceInfo, status: str, reason: str | None) -> StepOutcome:
        """Optional adapter hook: keep what a failed run left, moved aside with a manifest, instead of deleting it."""
        if not hasattr(self._adapter, "retain_run_data"):
            return StepOutcome(constants.PASS)
        return self._outcome("retain_run_data", device, status, reason)


# -- helpers ----------------------------------------------------------------------------------------------------------------


def unity_version(project_dir: Path | None) -> str:
    if project_dir is None:
        return "unknown"
    try:
        text = (project_dir / "ProjectSettings" / "ProjectVersion.txt").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = _UNITY_VERSION.search(text)
    return match.group(1) if match else "unknown"


def select_device(devices: Sequence[DeviceInfo], wanted: Mapping[str, Any], plan: Mapping[str, Any],
                  known: Sequence[DeviceInfo] = ()) -> tuple[DeviceInfo | None, str | None]:
    """Pick the device that satisfies the pipeline and the plan, or say why none does. `known` are the devices of the host's registry:
    they are never selected (discovery decides what is attached), they only tell an offline device from one that does not exist."""
    requires = plan.get("requires", {})
    allowed = requires.get("evidence")
    if plan.get("kind") == "performance":
        allowed = [e for e in (allowed or _PERFORMANCE_EVIDENCE) if e in _PERFORMANCE_EVIDENCE] or list(_PERFORMANCE_EVIDENCE)

    def matching(pool: Sequence[DeviceInfo]) -> list[DeviceInfo]:
        candidates = list(pool)
        if wanted.get("id"):
            candidates = [d for d in candidates if d.id == wanted["id"]]
        # A device that waits for an owner action (an unauthorized adb connection) reveals neither its class nor its capabilities,
        # so it is not filtered by them: the run reaches it and ends NEEDS_PERMISSION with the action, not a misleading "no device".
        waiting = [d for d in candidates if NEEDS_PERMISSION_CAPABILITY in d.capabilities]
        if wanted.get("class"):  # a waiting device whose class is known still has to match; only an unknown class is let through
            candidates = [d for d in candidates if d.device_class == wanted["class"] or (d in waiting and d.device_class.endswith("-unknown"))]
        candidates = [d for d in candidates if set(requires.get("capabilities", [])) <= set(d.capabilities) or d in waiting]
        if allowed:
            candidates = [d for d in candidates if d.evidence in allowed]
        return candidates

    candidates = matching(devices)
    if not candidates:
        needs = ", ".join(filter(None, [wanted.get("id"), wanted.get("class"), *requires.get("capabilities", []),
                                        f"evidence {'/'.join(allowed)}" if allowed else None])) or "any device"
        attached = {d.id for d in devices}
        offline = sorted(d.id for d in matching([d for d in known if d.id not in attached]))
        if offline:
            return None, (f"device offline: {', '.join(offline)} satisfies {needs} but is not attached or not reachable now "
                          "(check the cable, unlock it and trust this computer, then `valtrion devices --refresh`)")
        return None, f"no attached device satisfies: {needs} (no device with these settings is known on this host)"
    return sorted(candidates, key=lambda d: (NEEDS_PERMISSION_CAPABILITY in d.capabilities, d.id))[0], None


#: Folders of a run that are scratch (a build output, the device poll copy); never listed as artifacts.
_SCRATCH_FOLDERS = ("build", "device-poll", "device-before")


def _hash_artifacts(directory: Path, skip: Sequence[Path], include_top: bool = False) -> list[dict]:
    """Hash the immutable evidence of a run: files in its subfolders (per-run results, console logs, device output, screenshots).
    The live job files at the top (job.json, events.jsonl, spec) are rewritten after the result and are not listed, and neither
    is anything registered as temporary, which cleanup deletes."""
    skipped = {p.resolve() for p in skip}
    try:
        temporary = [Path(p) for p in core.read_json(directory / artifacts.TEMP_REGISTRY)]
    except (OSError, ValueError):
        temporary = []
    entries = []
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        relative_parts = path.relative_to(directory).parts
        if (len(relative_parts) < 2 and not include_top) or relative_parts[0] in _SCRATCH_FOLDERS or path.resolve() in skipped or path.name.startswith("."):
            continue
        if any(artifacts._is_within(path.resolve(), t.resolve()) for t in temporary):  # noqa: SLF001 - same package
            continue
        kind = "console" if path.name == "console.log" else "screenshot" if path.suffix in (".png", ".jpg") else \
            "result" if path.name == "result.json" else "diagnostic"
        entries.append({"kind": kind, "path": "/".join(relative_parts), "sha256": core.sha256_file(path)})
    return entries


def _latest_device_run(folder: Path, ignore: frozenset[str] = frozenset()) -> tuple[Path | None, dict | None, dict | None]:
    """The newest `<runId>` subfolder of a pulled `gdv/out` (not one of `ignore`, the runs the device held before this launch),
    with its heartbeat and result when readable."""
    runs = sorted((p for p in folder.glob("*") if p.is_dir() and p.name not in ignore),
                  key=lambda p: p.stat().st_mtime) if folder.is_dir() else []
    if not runs:
        return None, None, None
    latest = runs[-1]
    return latest, _read_optional(latest / "heartbeat.json"), _read_optional(latest / "result.json")


def _read_optional(path: Path) -> dict | None:
    try:
        document = core.read_json(path, attempts=2)
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def finish_run_data(adapter: Any, device: DeviceInfo, status: str, reason: str | None, unverified: list[str]) -> None:
    """What happens to the data a run left on its device (user data and test isolation): a passing run deletes it
    (`cleanup_run_data`); a failed or interrupted run keeps it as diagnostics with a manifest (`retain_run_data`), and its removal is
    left to the task that started the run after review, never to another run. An adapter without the hook leaves its data alone."""
    try:
        if status == constants.PASS:
            cleanup = getattr(adapter, "cleanup_run_data", None)
            outcome = cleanup(device) if cleanup else StepOutcome(constants.PASS)
            if not outcome.ok:
                unverified.append(f"the run's data could not be cleaned up: {outcome.reason}; what is left is for the task that "
                                  "started the run to remove")
            return
        retain = getattr(adapter, "retain_run_data", None)
        outcome = retain(device, status, reason) if retain else StepOutcome(constants.PASS)
    except Exception as error:  # noqa: BLE001 - adapter code; the result of the run must survive a failing cleanup
        unverified.append(f"the run's data could not be handled: {type(error).__name__}: {error}")
        return
    if not outcome.ok:
        unverified.append(f"the run's data could not be kept as diagnostics: {outcome.reason}")
    elif outcome.details.get("retainedAt"):
        unverified.append(f"the run's data is kept as diagnostics at {outcome.details['retainedAt']}; the task that started the run "
                          "removes it after review")


# -- device run -------------------------------------------------------------------------------------------------------------


@dataclass
class DeviceRun:
    status: str
    reason: str | None
    device_result: dict | None
    layers: dict[str, str]
    directory: Path
    started: str
    finished: str
    unverified: list[str] = field(default_factory=list)


NO_SHA = "0" * 40
#: A run executed over SSH records the project folder it tested as `host.tools["projectDir"]`.
PROJECT_TOOL_KEY = "projectDir"
PRELAUNCH_FOLDER = "prelaunch"


#: Names Windows treats as devices, whatever the extension; a copy to `prelaunch/<name>` would write to the device.
_RESERVED_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10)),
                             *(f"{stem}{digit}" for stem in ("COM", "LPT") for digit in "¹²³")})
#: The harness' own folder in the data root (inbox, output, ledger): a pre-launch file must not replace the plan or seed a result.
_HARNESS_ROOT = "gdv"


def prelaunch_target(remote: object) -> str | None:
    """The device path of a pre-launch file, or None when it is not a plain relative path inside the application's data root
    (no drive, UNC, absolute, `..`, empty or device-named segment, and nothing in the harness folder `gdv/`)."""
    if not isinstance(remote, str) or not remote or "\\" in remote or ":" in remote or remote.startswith("/"):
        return None
    parts = remote.split("/")
    if parts[0].casefold() == _HARNESS_ROOT:  # the data roots of Windows and macOS ignore case
        return None
    for part in parts:
        if part in ("", ".", "..") or part != part.rstrip(". ") or "$" in part or part.split(".")[0].rstrip(" ").upper() in _RESERVED_NAMES:
            return None
    return remote


def validate_prelaunch(stages: Sequence[Mapping[str, Any]], base: Path | None) -> str | None:
    """The first problem with any stage's `prelaunchFiles` (a bad target, a missing local file, two entries for one target or one
    entry inside another), checked before anything starts so a typo does not cost a build."""
    for stage in stages:
        targets: list[str] = []
        for entry in stage.get("prelaunchFiles") or ():
            remote = prelaunch_target(entry.get("remote"))
            if remote is None:
                return f"prelaunch file target {entry.get('remote')!r} must be a relative path inside the application data root (not in gdv/)"
            folded = remote.casefold()
            if any(folded == other or folded.startswith(other + "/") or other.startswith(folded + "/") for other in targets):
                return f"prelaunch file target {remote} collides with another entry of the same stage"
            targets.append(folded)
            local = Path(str(entry.get("local", "")))
            if not local.is_absolute() and base is not None:
                local = base / local
            if not local.is_file():
                return f"prelaunch file {local} does not exist on the host that runs the pipeline"
    return None


def push_prelaunch_files(
        adapter: PlatformAdapter, device: DeviceInfo, entries: Sequence[Mapping[str, Any]] | None, base: Path | None,
        directory: Path) -> tuple[str, str] | None:
    """Push the stage's `prelaunchFiles` (`{local, remote}`) into the application's data root before it starts: the application reads
    them before its first scene (a camera configuration, a diagnostics switch). `local` is absolute or relative to the pipeline file;
    `remote` is relative to the data root. Each file is also copied to `<run>/prelaunch/<remote>`, so the result lists what the run
    started with. Returns `(status, reason)` of the first problem, or None."""
    for entry in entries or ():
        remote = prelaunch_target(entry.get("remote"))
        if remote is None:
            return constants.BLOCKED, f"prelaunch file target {entry.get('remote')!r} must be a relative path inside the application data root"
        local = Path(str(entry.get("local", "")))
        if not local.is_absolute() and base is not None:
            local = base / local
        if not local.is_file():
            return constants.BLOCKED, f"prelaunch file {local} does not exist"
        kept = directory / PRELAUNCH_FOLDER / remote
        try:
            kept.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(local, kept)
        except OSError as error:
            return constants.BLOCKED, f"prelaunch file {remote} could not be kept in the run folder: {error}"
        pushed = adapter.push(device, local, remote)
        if not pushed.ok:
            return pushed.status, f"push of the prelaunch file {remote} failed: {pushed.reason}"
    return None


def run_on_device(
        adapter: PlatformAdapter, device: DeviceInfo, plan_path: Path, plan: Mapping[str, Any], stage: Mapping[str, Any],
        directory: Path, options: PipelineOptions, cancelled: Callable[[], bool]) -> DeviceRun:
    """Push the plan, launch the application and watch the device until it reports a result, stalls or runs out of time."""
    directory.mkdir(parents=True, exist_ok=True)
    started = core.utc_now()
    layers: dict[str, str] = {}
    unverified: list[str] = []
    args = stage.get("args", {})
    app = str(args.get("app", ""))
    console = directory / "console.log"

    def stop(status: str, reason: str) -> DeviceRun:
        # Nothing is running on this path (the run ended before or at the launch), but a push may have left data behind. The run
        # failed, so that data is kept as diagnostics (see `finish_run_data`).
        if layers.get("launch") != constants.PASS:
            finish_run_data(adapter, device, status, reason, unverified)
        return DeviceRun(status, reason, None, layers, directory, started, core.utc_now(), unverified)

    if not app:
        return stop(constants.BLOCKED, "the run stage names no application (stage.args.app)")
    adapter.bind_application(app)
    health = adapter.health(device)
    if health.permission_needed:
        return stop(constants.NEEDS_PERMISSION, health.permission_needed)
    if not health.reachable:
        return stop(constants.BLOCKED, f"device {device.id} is not reachable: {'; '.join(health.notes) or 'no detail'}")
    if health.locked:
        return stop(constants.NEEDS_PERMISSION, f"device {device.id} is locked; unlock it (owner action)")
    if health.thermal_state is None:
        unverified.append("thermal state not reported by the platform")
    if health.locked is None:
        unverified.append("lock state not reported by the platform")
    prepared = adapter.prepare(device, app, plan.get("dataPolicy"))
    if not prepared.ok:
        return stop(prepared.status, f"preparing the application failed: {prepared.reason}")
    pushed = adapter.push(device, plan_path, DEVICE_INBOX)
    if not pushed.ok:
        return stop(pushed.status, f"push of the plan failed: {pushed.reason}")
    failed = push_prelaunch_files(adapter, device, stage.get("prelaunchFiles"), options.pipeline_dir, directory)
    if failed is not None:
        return stop(*failed)
    # What the device already holds belongs to earlier runs; only a run folder that appears after the launch is this run's.
    earlier = directory / "device-before"
    previous: frozenset[str] = frozenset()
    listing = adapter.pull(device, DEVICE_OUT, earlier)
    if listing.ok and earlier.is_dir():
        previous = frozenset(p.name for p in earlier.iterdir() if p.is_dir())
    elif not listing.ok:
        # No output folder yet is normal on a first run, but a failed listing could hide an earlier result: say so.
        unverified.append(f"the device output could not be listed before the launch ({listing.reason}); an earlier run's result is not excluded")
    launched = adapter.launch(device, app, [str(a) for a in args.get("launchArgs", [])], console)
    layers["launch"] = launched.status
    if not launched.ok:
        adapter.terminate(device, app)  # best effort: a launch that failed late may have left the application or a capture running
        return stop(launched.status, f"launch failed: {launched.reason}")

    # The plan's budget is the time the plan itself needs; a platform that must start an Editor first adds its startup allowance.
    budget = float(stage.get("timeoutSec") or plan["budgetSeconds"] + float(getattr(adapter, "startup_allowance_sec", 0.0)))
    stall = float(args.get("stallSec") or supervise.DEFAULT_STALL_SEC)
    polled = directory / "device-poll"
    watchdog = supervise.Watchdog(budget, stall, heartbeat=None, log_counts=False)
    began = options.monotonic()
    last_seq: int | None = None
    last_progress = began
    status: str | None = None
    reason: str | None = None
    device_result: dict | None = None
    while status is None:
        if cancelled():
            status, reason = constants.BLOCKED, "cancelled by request"
            break
        pulled = adapter.pull(device, DEVICE_OUT, polled)
        _, heartbeat, result = _latest_device_run(polled, previous) if pulled.ok else (None, None, None)
        if heartbeat is not None and isinstance(heartbeat.get("seq"), int) and (last_seq is None or heartbeat["seq"] > last_seq):
            last_seq, last_progress = heartbeat["seq"], options.monotonic()
        if result is not None and result.get("status") in constants.STATUSES:
            device_result, status = result, result["status"]
            reason = result.get("reason")
            break
        marker = supervise.read_marker(console)
        if marker is not None and marker[0] != constants.PASS and (result is None):
            status, reason = marker[0], marker[1] or f"the application reported {marker[0]}"
            break
        now = options.monotonic()
        if now - began >= watchdog.timeout_sec:
            status, reason = constants.TIMEOUT, f"no result within the {budget:g} s budget of the plan"
        elif now - last_progress >= watchdog.stall_sec:
            status = constants.STALL
            reason = f"the heartbeat did not advance for {now - last_progress:.0f} s (stall window {stall:g} s; last seq {last_seq})"
        else:
            options.sleep(options.device_poll_sec)
    # Collect what the device has, whatever the outcome, before the application is stopped.
    final = adapter.pull(device, DEVICE_OUT, directory / "device")
    if not final.ok:
        unverified.append(f"device output could not be collected: {final.reason}")
    elif device_result is None:
        _, _, device_result = _latest_device_run(directory / "device", previous)
    stopped = adapter.terminate(device, app)
    if not stopped.ok:
        unverified.append(f"the application could not be stopped: {stopped.reason}")
    if status not in (constants.PASS,):
        shot = adapter.screenshot(device, directory / "screenshots" / "failure.png")
        if not shot.ok:
            unverified.append(f"no screenshot of the failure: {shot.reason}")
    after = adapter.health(device)
    if not stopped.ok:
        unverified.append("the application is still running, so its data folder is left in place")
    else:
        finish_run_data(adapter, device, status, reason, unverified)
    if after.thermal_state:
        unverified.append(f"thermal state after the run: {after.thermal_state} (not controlled)")
    layers["scenario"] = status
    return DeviceRun(status, reason, device_result, layers, directory, started, core.utc_now(), unverified)


def assemble_result(
        run_id: str, outcome: DeviceRun, pipeline: Mapping[str, Any], plan: Mapping[str, Any], plan_sha256: str,
        device: DeviceInfo | None, host: Mapping[str, Any], unity: str, dirty: bool, extra_layers: Mapping[str, str],
        extra_unverified: Sequence[str] = (), build: Mapping[str, Any] | None = None, checkout: Mapping[str, Any] | None = None) -> dict:
    """Build the `gdv.result/1` of one run from what the host observed and what the harness reported. `build` is the published
    (or reused) build artifact of the job, when it has one; `checkout` says which commit the job's build stage had checked out in
    which folder, when it did."""
    candidate = pipeline["candidate"]
    device_result = outcome.device_result or {}
    status, reason = outcome.status, outcome.reason
    if status == constants.PASS:
        reason = None
    elif not reason:
        reason = f"the run ended {status}"
    samples: dict[str, list[float]] = {}
    units: dict[str, str] = {}
    for metric in device_result.get("metrics", []):
        samples.setdefault(metric["name"], []).extend(metric.get("samples", []))
        units[metric["name"]] = metric.get("unit", plan.get("metrics", {}).get(metric["name"], {}).get("unit", "1"))
    metrics = [{"name": name, "unit": units[name], "samples": values, "stats": compare.compute_stats(values)} for name, values in samples.items()]
    target: dict[str, Any] = {
        "platform": device.platform if device else pipeline["device"].get("platform", "editor"),
        "deviceClass": device.device_class if device else pipeline["device"].get("class", "unknown"),
        "osMajor": device.os_major if device else 0, "evidence": device.evidence if device else constants.EVIDENCE_HOST}
    if device:
        target.update({"deviceId": device.id, "model": device.model, "os": device.os_version})
    layers = {**extra_layers, **outcome.layers}
    document: dict[str, Any] = {
        "schema": "gdv.result/1", "runId": run_id, "status": status, "reason": reason,
        "candidate": {"repo": candidate["repo"], "sha": candidate["sha"], "profile": candidate["profile"], "unity": unity, "dirty": dirty},
        "target": target, "host": dict(host), "plan": {"id": plan["id"], "sha256": plan_sha256},
        "startedUtc": outcome.started, "finishedUtc": outcome.finished, "layers": layers,
        "steps": [s for s in device_result.get("steps", []) if isinstance(s, dict)], "metrics": metrics, "artifacts": [],
        "unverified": [*device_result.get("unverified", []), *outcome.unverified, *extra_unverified]}
    if build:
        document["build"] = dict(build)
    if checkout:
        document["checkout"] = dict(checkout)
    return document


def finalize_result(document: dict, directory: Path, destination: Path, validator: Validator | None = None,
                    include_top: bool = False) -> dict:
    """Hash the artifacts, validate, write. An invalid result is not written as result.json: it becomes a FAIL that says why."""
    document["artifacts"] = _hash_artifacts(directory, [destination], include_top)
    _, errors = validate_document(document, validator or Validator())
    if errors:
        rejected = destination.with_name("result.rejected.json")
        core.atomic_write_json(rejected, document)
        document = {**{k: v for k, v in document.items() if k != "comparison"}, "status": constants.FAIL,
                    "reason": "the result violates gdv.result/1: " + "; ".join(errors[:5]), "layers": {"scenario": constants.FAIL}}
        _, again = validate_document(document, validator or Validator())
        if again:
            raise ValueError("cannot build a valid failure result: " + "; ".join(again[:5]))
    core.atomic_write_json(destination, document)
    return document


# -- stages -----------------------------------------------------------------------------------------------------------------


def _stage_outcome_layers(prefix: str, outcome: StepOutcome) -> dict[str, str]:
    return {key[len("layer."):]: value for key, value in outcome.details.items() if key.startswith("layer.") and value in constants.STATUSES}


def execute(pipeline: Mapping[str, Any], pipeline_path: Path | None, ctx: JobContext, options: PipelineOptions) -> PipelineOutcome:
    host = ctx.spec["host"]
    events = ctx.events
    directory = ctx.store.directory
    base = options.pipeline_dir or (pipeline_path.parent if pipeline_path else directory)
    project_dir = options.project_dir
    unity, dirty = unity_version(project_dir), True  # an unknown tree is never claimed to be clean
    head_note: list[str] = []
    if host.get("transport") == "ssh" and project_dir is not None:
        # A run sent by a caller: the caller did not choose this path, so the result says which folder was tested
        # (host.tools is a free string map in gdv.result/1 and in its readers).
        host = {**host, "tools": {**host.get("tools", {}), PROJECT_TOOL_KEY: str(project_dir)}}
    requested = pipeline["candidate"].get("sha")
    unspecified = not requested or set(requested) == {"0"}  # a committed pipeline file cannot name its own commit
    candidate_sha = requested
    own = [Path(ctx.spec[key]) for key in ("artifactsRoot", "workRoot") if ctx.spec.get(key)]
    #: A managed checkout (`--checkout`) is reset to the job's commit when the `build` stage starts, under its lease; any other folder is
    #: checked then to be at the commit (`checkout.py`). Jobs of other commits may use the folder while this one waits in the queue.
    managed = options.checkout_id is not None and options.checkout_root is not None
    has_build = any(s["stage"] == "build" for s in pipeline["stages"])
    if managed and (project_dir is None or unspecified or not has_build):
        return PipelineOutcome(constants.BLOCKED, "a managed checkout needs a build stage and the commit to build (--sha)")
    enforce_checkout = bool(options.sha_pinned and project_dir is not None and not unspecified and has_build)
    checkout_info: dict[str, Any] | None = None
    if managed:
        dirty = True  # until the build stage has checked the commit out and read the tree
    elif project_dir is not None:
        # The sha in the result must be the commit that was actually tested, not a label given on the command line. The state is read
        # now, before any stage, and the folders this run keeps (artifacts, work root) are not part of the tree being judged.
        state = gitstate.read_state(project_dir, options.git, [*own, directory])
        dirty, head, readable = state.dirty, state.sha, state.sha is not None
        head_note.extend(state.notes)
        if unspecified:
            candidate_sha = head if readable else NO_SHA
            if not readable:
                dirty = True
                head_note.append("the result carries no commit and cannot become a baseline")
        elif not readable or head != requested:
            reason = (f"the checkout is at {head[:12] if readable else 'an unreadable commit'}, not the candidate {requested[:12]}")
            if options.sha_pinned:
                return PipelineOutcome(constants.BLOCKED, reason + "; --sha must name the commit that is checked out")
            else:
                dirty = True
                head_note.append(f"{reason}; the result is marked dirty and cannot become a baseline")
    elif unspecified:
        candidate_sha = NO_SHA
        dirty = True
        head_note.append("the pipeline names no commit and there is no checkout to read it from; the result carries no commit "
                         "and cannot become a baseline")
    elif options.sha_pinned:
        head_note.append("--sha was given but there is no checkout (--project-dir) to verify it against")
    pipeline = {**pipeline, "candidate": {**pipeline["candidate"], "sha": candidate_sha}}  # everything below records the real commit
    problem = validate_prelaunch(pipeline["stages"], base)
    if problem is not None:
        return PipelineOutcome(constants.BLOCKED, problem)  # before any stage: a typo must not cost a build

    plans: dict[str, tuple[Path, dict, str]] = {}

    def load_plan(name: str) -> tuple[Path, dict, str]:
        if name not in plans:
            path = (base / name) if not Path(name).is_absolute() else Path(name)
            data = path.read_bytes()
            document = json.loads(data.decode("utf-8-sig"))
            _, errors = validate_document(document)
            if errors:
                raise ValueError(f"plan {name} is invalid: " + "; ".join(errors[:3]))
            plans[name] = (path, document, core.sha256_bytes(data))
        return plans[name]

    first_plan = next((s["plan"] for s in pipeline["stages"] if s["stage"] == "run" and s.get("plan")), None)
    platform = pipeline["device"].get("platform")
    try:
        if first_plan and not platform:
            platform = (load_plan(first_plan)[1].get("requires", {}).get("platforms") or [None])[0]
    except (OSError, ValueError) as error:
        return PipelineOutcome(constants.BLOCKED, f"cannot read the plan {first_plan}: {error}")
    if not platform:
        return PipelineOutcome(constants.BLOCKED, "the pipeline does not say which platform it targets (device.platform or a plan's requires.platforms)")
    raw_adapter = options.adapter_factory(platform)
    if raw_adapter is not None and project_dir is not None and hasattr(raw_adapter, "use_project"):
        raw_adapter.use_project(project_dir)  # an adapter that reads the project's profiles (Editor, Windows) learns where it is
    adapter = GuardedAdapter(raw_adapter, options.op_timeout_sec) if raw_adapter is not None else None
    if managed and raw_adapter is not None and not getattr(raw_adapter, "build_uses_editor", True):
        return PipelineOutcome(constants.BLOCKED, f"a managed checkout is for platforms that build in the Editor and run the built app; "
                               f"{platform} runs from the project folder itself, which another job could switch before it starts")
    if adapter is None:
        return PipelineOutcome(constants.BLOCKED, f"no adapter for platform {platform} is installed on host {host['id']}"
                               + (f" ({providers.load_errors().get(platform)})" if providers.load_errors().get(platform) else ""))

    device: DeviceInfo | None = None
    device_leased = False
    artifact: Path | None = None
    build_outcome: StepOutcome | None = None
    build_info: dict[str, Any] | None = None  # the published build (or the reused one), recorded in every result of the job
    reaped_dead_jobs = False  # the leftovers of cancelled jobs are removed before this job's first build only
    build_pending_release = False  # a build whose output is still in the work folder (not published): released after the install
    used_entry: buildcache.Entry | None = None  # touched again before the install, so a long wait for the device does not let gc take it
    extra_layers: dict[str, str] = {}
    stage_results: list[dict] = []
    series_plans: dict[str, dict] = {}  # runId of a run stage's result -> the plan document it ran
    overall = PipelineOutcome(constants.PASS, None)
    stages = pipeline["stages"]
    #: Stages that need the device: its lease is held from the first `install`/`run` to the last `install`/`run`/`collect`.
    last_device_index = max((i for i, s in enumerate(stages) if s["stage"] in DEVICE_STAGES), default=-1)
    lease_wait = float(ctx.spec.get("leaseWaitSec", 0))
    runs_a_plan = any(s["stage"] == "run" for s in stages)

    def early(status: str, reason: str, on: DeviceInfo | None = None) -> dict:
        return _early_result(ctx, pipeline, plans, load_plan, first_plan, on or device, host, unity, dirty, extra_layers, status, reason,
                             head_note, build_info, checkout_info)

    def release_device() -> None:
        nonlocal device_leased
        if device_leased and device is not None:
            device_leased = False
            ctx.release({"resource": f"device:{device.id}"})
            ctx.set_phase(None)

    def conclude() -> PipelineOutcome:
        """Write the job-level result: the first non-passing stage result, otherwise the last one. Runs on every way out, so a
        pipeline that stopped early (a failed build, a busy device) still leaves a result that says why."""
        release_device()
        nonlocal build_pending_release
        if build_pending_release:
            # A build that could not be published may be run from its artifact (a Windows player, a macOS app): its output is
            # released when the last stage is done, not after the install. After a failure it stays for diagnosis, as before.
            build_pending_release = False
            if overall.status == constants.PASS:
                release_build_output()
        if not stage_results and (overall.status != constants.PASS or not runs_a_plan):
            # Stopped before any stage produced a result (a timeout or cancel while building, a missing project), or a pipeline
            # with no run stage (build only, install only): still say what happened.
            stage_results.append(early(overall.status, overall.reason or f"the pipeline ended {overall.status}") if overall.status != constants.PASS
                                 else early(constants.PASS, ""))
        if not stage_results:
            return overall
        if pipeline.get("compare"):
            # Every passing series is compared with the baseline of its own plan; none is skipped because another one failed or came last.
            folders = baseline_folders(pipeline["compare"], base, project_dir)
            for series in stage_results:
                if series["status"] == constants.PASS and series["runId"] in series_plans:
                    _attach_comparison(series, series_plans[series["runId"]], folders)
        core.atomic_write_json(directory / "summary.json", [_summary_entry(r) for r in stage_results])
        chosen = finalize_result(_with_other_series(_worst_result(stage_results), stage_results), directory, ctx.store.result)
        overall.results = stage_results
        if overall.status == constants.PASS:
            overall.status, overall.reason = _job_status(chosen, stage_results)
        return overall

    def fail(status: str, reason: str) -> PipelineOutcome:
        overall.status, overall.reason = status, reason
        return conclude()

    def select(name: str) -> PipelineOutcome | None:
        """Choose the device from discovery, without taking its lease; a concluded PipelineOutcome (BLOCKED) when none can serve the
        pipeline. This is the fast failure for a device that cannot be served at all (unknown, offline, wrong class)."""
        nonlocal device
        if device is not None:
            return None
        plan_for_device = load_plan(first_plan)[1] if first_plan else {}
        try:
            known = [d for d in config.registry_devices(config.load_registry(ctx.home)) if d.platform == platform]
        except Exception:  # noqa: BLE001 - the registry only improves the reason; a broken one must not stop selection
            known = []
        try:
            found, why = select_device(adapter.discover(), pipeline["device"], plan_for_device, known)
        except Exception as error:  # noqa: BLE001 - discovery is adapter code
            found, why = None, f"device discovery failed: {type(error).__name__}: {error}"
        if found is None:
            events.emit("step.finished", why, step=name, status=constants.BLOCKED)
            stage_results.append(early(constants.BLOCKED, why or "no device", None))
            return fail(constants.BLOCKED, why or "no device")
        device = found
        adapter.bind_device(found)
        return None

    def preselect_device(name: str) -> PipelineOutcome | None:
        """Before a build that is followed by device stages: pick the device and, when the job does not wait for leases, refuse at once
        if someone holds it, so the request fails before the build and not after it. Nothing is leased here."""
        blocked = select(name)
        if blocked is not None:
            return blocked
        assert device is not None
        if lease_wait <= 0:
            held = ctx.probe(f"device:{device.id}")
            if held:
                events.emit("step.finished", held, step=name, status=constants.BLOCKED)
                stage_results.append(early(constants.BLOCKED, held))
                return fail(constants.BLOCKED, held)
        return None

    def claim_device(name: str) -> PipelineOutcome | None:
        """Take the device lease for the device stages (`install`, `run`, `collect`); a concluded PipelineOutcome when that is not
        possible. The lease is taken only here, never while the job holds the project or the editor slot (see the build stage)."""
        nonlocal device_leased
        if device_leased:
            return None
        blocked = select(name)
        if blocked is not None:
            return blocked
        assert device is not None
        ctx.set_phase("waiting-device")
        reason = ctx.acquire({"resource": f"device:{device.id}"})
        if reason:
            ctx.set_phase(None)
            events.emit("step.finished", reason, step=name, status=constants.BLOCKED)
            stage_results.append(early(constants.BLOCKED, reason))
            return fail(constants.BLOCKED, reason)
        device_leased = True
        ctx.set_phase("on-device")
        return None

    def check_disk(name: str) -> PipelineOutcome | None:
        """Before the build starts: with `disk.minFreeGiB` set, clean up when the work root's volume is low on space, and end the
        stage BLOCKED (no toolchain started) when it still is. Without the setting nothing happens."""
        try:
            settings = config.load_host_settings(ctx.home)
        except config.ConfigError as error:
            head_note.append(f"the free-space check was skipped: {error}")
            return None
        if not settings.min_free_gib:
            return None
        outcome = diskspace.ensure_free(settings, ctx.home, ctx.spec.get("protectedPaths", []))
        if outcome.removed:
            events.emit("log", f"low disk space ({outcome.free_before / diskspace.GIB:.1f} GiB free): removed {len(outcome.removed)} "
                        f"abandoned or expired item(s), {outcome.free_after / diskspace.GIB:.1f} GiB free now",
                        data={"removed": list(outcome.removed)})
        if outcome.ok:
            return None
        events.emit("step.finished", outcome.reason, step=name, status=constants.BLOCKED)
        stage_results.append(early(constants.BLOCKED, outcome.reason or "low disk space"))
        return fail(constants.BLOCKED, outcome.reason or "low disk space")

    def wait_for_disk(name: str) -> PipelineOutcome | None:
        """Before the build takes the project and Editor leases: with a disk wait (`--disk-wait`, else host.json `disk.waitSec`) and
        `disk.minFreeGiB`, wait for the free space to appear, cleaning up and checking again every poll. The job holds no lease while it
        waits, so it cannot keep the project or the slot from a job whose end would free the space; the check under the leases
        (`check_disk`) stays as the last word. BLOCKED when no space appears in time, or at once when waiting cannot help."""
        try:
            settings = config.load_host_settings(ctx.home)
        except config.ConfigError:
            return None  # `check_disk` reports the broken file
        wanted = ctx.spec.get("diskWaitSec")
        wait = settings.disk_wait_sec if wanted is None else float(wanted)
        if not settings.min_free_gib or wait <= 0:
            return None

        def waiting(outcome: diskspace.Outcome, waited: float) -> None:
            ctx.set_phase("waiting-disk")
            events.emit("log", f"waiting for disk space: {outcome.free_after / diskspace.GIB:.1f} GiB free, {settings.min_free_gib:g} GiB "
                        f"minimum; waited {waited:.0f} s of {wait:g} s", step=name,
                        data={"freeBytes": outcome.free_after, "minFreeBytes": int(settings.min_free_gib * diskspace.GIB),
                              "waitedSec": round(waited, 1)})

        try:
            with ctx.wait_clock():
                outcome = diskspace.wait_for_space(settings, ctx.home, ctx.spec.get("protectedPaths", []), wait, ctx.job_id,
                                                   on_wait=waiting, cancelled=ctx.cancelled)
        finally:
            ctx.set_phase(None)
        if outcome.removed:
            events.emit("log", f"low disk space: removed {len(outcome.removed)} abandoned or expired item(s), "
                        f"{outcome.free_after / diskspace.GIB:.1f} GiB free now", data={"removed": list(outcome.removed)})
        if outcome.ok:
            return None
        if outcome.cancelled:
            overall.cancelled = True
            return fail(constants.BLOCKED, "cancelled by request")
        events.emit("step.finished", outcome.reason, step=name, status=constants.BLOCKED)
        stage_results.append(early(constants.BLOCKED, outcome.reason or "low disk space"))
        return fail(constants.BLOCKED, outcome.reason or "low disk space")

    def adopt_cached(cached: buildcache.Entry, name: str) -> None:
        """The job uses a build from the cache instead of building."""
        nonlocal artifact, used_entry, build_outcome, build_info
        buildcache.touch(cached, source=_build_source(pipeline["candidate"]["repo"], options.checkout_id, project_dir))
        artifact, used_entry, build_outcome = cached.path, cached, StepOutcome(constants.PASS)
        build_info = {**cached.describe(), "reused": True, "builtByJob": cached.manifest.get("builtByJob", "unknown")}
        diskspace.remember_build(directory, cached.key)
        head_note.append(f"the build was reused from the cache (key {cached.key}, built by job {build_info['builtByJob']}); "
                         "no build ran for this job")
        events.emit("progress", f"build reused from cache {cached.key}", step=name, data={"build": build_info})
        events.emit("step.finished", "reused from the build cache", step=name, status=constants.PASS)

    def check_out(name: str) -> PipelineOutcome | None:
        """The job's commit is in the project folder (the caller holds the folder's `unity-project` lease for a managed checkout, and
        for any build in the Editor). A managed checkout is reset to the commit; any other folder must already be at it. Ends the stage
        BLOCKED when that is not so; afterwards the tree is read again, so the dirty flag and the Unity version are the commit's own."""
        nonlocal dirty, unity, checkout_info
        assert project_dir is not None and candidate_sha
        if managed:
            assert options.checkout_root is not None
            slow = None if options.git is not compare.run_git else (lambda folder, arguments: compare.run_git(folder, arguments, timeout=1800))
            done = checkout.ensure(options.checkout_root, candidate_sha, options.git, remote=options.checkout_remote,
                                   keep=[*own, directory], slow_git=slow, cancelled=ctx.cancelled, unity_path=options.unity_path)
            problem = None if done.ok else f"the commit {candidate_sha[:12]} cannot be checked out in {options.checkout_root}: {done.reason}"
            record = {**done.describe(options.checkout_id), "folder": str(project_dir)}
            if done.created:
                events.emit("log", f"created the managed checkout {options.checkout_id} in {options.checkout_root}", step=name)
            if done.ok:
                unmarked = checkout.mark_managed(options.checkout_root, options.git)
                if unmarked:
                    events.emit("log", f"the managed checkout has no marker file: {unmarked}", step=name)
                try:
                    checkout.remember(ctx.home or core.valtrion_home(), options.checkout_root, options.checkout_id, done.sha)
                except OSError as error:  # the record only feeds `doctor`; it never stops a build
                    events.emit("log", f"cannot record the use of the managed checkout: {error}", step=name)
            if done.removed:
                events.emit("log", f"removed stray build output from the checkout: {', '.join(done.removed)}", step=name,
                            data={"removed": list(done.removed)})
            if done.ok and done.switched:
                note = (f"checkout {options.checkout_id}: reset to {candidate_sha[:12]} in {project_dir} (it was at "
                        f"{(done.previous or 'no commit')[:12]})" + ("; the commit was fetched from the remote first" if done.fetched else ""))
                head_note.append(note)
                events.emit("log", note, step=name, data={"checkout": record})
        else:
            problem = checkout.verify_head(project_dir, candidate_sha, options.git)
            record = {"folder": str(project_dir), "sha": candidate_sha, "switched": False, "fetched": False}
        if problem:
            events.emit("step.finished", problem, step=name, status=constants.BLOCKED)
            stage_results.append(early(constants.BLOCKED, problem))
            return fail(constants.BLOCKED, problem)
        checkout_info = record
        state = gitstate.read_state(project_dir, options.git, [*own, directory])
        dirty = state.dirty
        head_note.extend(state.notes)
        unity = unity_version(project_dir)
        if raw_adapter is not None and hasattr(raw_adapter, "use_project"):
            raw_adapter.use_project(project_dir)  # profiles may differ between commits
        return None

    def release_build_output() -> None:
        """The build output in the job's work folder is not needed once it is published or installed: remove it now instead of at
        the end of the job (several finished builds that wait for the device would otherwise hold several GiB each)."""
        try:
            blocker = jobs_blocker(ctx.spec)
        except (KeyError, OSError):
            blocker = None
        if blocker and not blocker.startswith("results are not saved"):
            return  # cleanup is off, the run is retained or a protected path overlaps: the work folder stays as the user asked
        try:
            listed = [Path(item) for item in core.read_json(directory / artifacts.TEMP_REGISTRY)]
        except (OSError, ValueError):
            listed = []
        base = ctx.work.base.resolve() if ctx.work is not None else None
        scratch = (directory / "build").resolve()
        targets = [path for path in listed if (base is not None and path.resolve() != base and artifacts._is_within(path.resolve(), base))  # noqa: SLF001
                   or path.resolve() == scratch]
        freed, errors = diskspace.release_paths(targets)
        if freed or errors:
            events.emit("log", f"released {freed / (1 << 20):.0f} MiB of build output from the job work folder"
                        + (f"; could not remove: {'; '.join(errors)}" if errors else ""), data={"freedBytes": freed})

    config_hash_seen: list[str | None] = [None]  # the profile settings the cache key was made from (read under the lease)

    def reuse_or_none(stage_args: Mapping[str, Any]) -> tuple[buildcache.Entry | None, str | None, Mapping[str, str]]:
        """(cached build or None, cache key or None, toolchain) for the build stage. No key means the build cannot be cached."""
        toolchain: dict[str, str] = {"unity": unity}
        try:
            toolchain.update(adapter.toolchain())
        except Exception as error:  # noqa: BLE001 - adapter code; without its toolchain the build is not cached
            head_note.append(f"the build cannot be cached: the adapter toolchain is unknown ({type(error).__name__}: {error})")
            return None, None, toolchain
        assert project_dir is not None
        if dirty or candidate_sha in (None, NO_SHA):
            return None, buildcache.dirty_key(ctx.job_id), toolchain
        config_hash_seen[0] = buildcache.profile_config_hash(project_dir, pipeline["candidate"]["profile"])
        key = buildcache.compute_key(
            pipeline["candidate"]["repo"], platform, candidate_sha, pipeline["candidate"]["profile"], toolchain, config_hash_seen[0],
            device.id if device is not None and adapter.signs_for_device else None)
        if stage_args.get("reuse") is False:
            return None, key, toolchain
        return buildcache.lookup(buildcache.builds_root(ctx.home), key), key, toolchain

    for index, stage in enumerate(stages):
        if ctx.cancelled():
            overall.cancelled = True
            return fail(constants.BLOCKED, "cancelled by request")
        name = f"{index + 1}:{stage['stage']}"
        events.emit("step.started", name, step=name)
        ctx.flush()
        kind = stage["stage"]
        if device_leased and (index > last_device_index or kind == "build"):
            release_device()  # the device is given back right after the last device stage, and never held into a build
        if kind == "build":
            if any(later["stage"] in DEVICE_CLAIM_STAGES for later in stages[index + 1:]):
                blocked = preselect_device(name)
                if blocked is not None:
                    return blocked
            if project_dir is None:
                return fail(constants.BLOCKED, "a build stage needs --project-dir")
            if device is None and adapter.signs_for_device and (pipeline["device"].get("id") or pipeline["device"].get("class")):
                blocked = select(name)  # a build-only pipeline that names the device it is signed for (nothing is leased)
                if blocked is not None:
                    return blocked
            profile = pipeline["candidate"]["profile"]
            # With a commit to check out, the cache key is read after the checkout (the profile settings are the commit's own).
            cached, cache_key, toolchain = (None, None, {"unity": unity}) if managed else reuse_or_none(stage.get("args", {}))
            if cached is not None:
                adopt_cached(cached, name)
                continue
            # The build holds the unity-project lease and an editor slot for this stage only; both are released as soon as it ends,
            # so another job can build while this one is on the device, and no job holds them while it waits for a device. A
            # platform with nothing to build in the Editor (the Editor target itself) takes its slot where it launches. The lease
            # order inside the stage is always project, then slot, and the slots are the host's, not the platform's.
            blocked = wait_for_disk(name)
            if blocked is not None:
                return blocked
            reason = None
            taken: list[dict[str, Any]] = []
            lost_before = set(ctx.lost_leases())  # `lost_leases` is cumulative: only losses after this point concern this build
            ctx.set_phase("waiting-build")
            build_outcome = None  # this stage's outcome only (the finally below reads it)
            try:
                if adapter.build_uses_editor or managed:
                    # The checkout moves the project folder, so it too is done only under the folder's lease.
                    try:
                        capacity = lease.unity_editor_capacity(ctx.home) if adapter.build_uses_editor else 0
                    except ValueError as error:
                        reason = str(error)
                    else:
                        wanted = [{"resource": lease.unity_project_resource(project_dir)}]
                        if adapter.build_uses_editor:
                            wanted.append({"resource": lease.UNITY_EDITOR_RESOURCE, "slots": capacity})
                        for item in wanted:
                            reason = ctx.acquire(item)
                            if reason:
                                break
                            taken.append(item)
                if reason:
                    events.emit("step.finished", reason, step=name, status=constants.BLOCKED)
                    return fail(constants.BLOCKED, reason)
                if adapter.build_uses_editor and not reaped_dead_jobs:  # only then the project lease is held; once per job
                    reaped_dead_jobs = True
                    try:
                        jobs_reap(Path(ctx.spec["artifactsRoot"]), ctx.spec["project"], project_dir, events, ctx.job_id)
                    except (KeyError, OSError):
                        pass
                if enforce_checkout:
                    blocked = check_out(name)
                    if blocked is not None:
                        return blocked
                    if managed:
                        cached, cache_key, toolchain = reuse_or_none(stage.get("args", {}))
                        if cached is not None:
                            adopt_cached(cached, name)  # the leases are released below; no build is needed
                            continue
                if raw_adapter is not None and hasattr(raw_adapter, "bind_incremental"):
                    # A managed checkout keeps its export, DerivedData and IL2CPP cache between builds (incremental.py); anything else
                    # builds from nothing as before. The key says when the kept state may no longer be used.
                    plan = None
                    if managed:
                        profile_key = buildcache.compute_key(
                            pipeline["candidate"]["repo"], platform, "incremental", profile, toolchain,
                            buildcache.profile_config_hash(project_dir, profile),
                            device.id if device is not None and adapter.signs_for_device else None)
                        plan = incremental.Plan(incremental.folder_of(ctx.home, pipeline["candidate"]["repo"], options.checkout_id or "default"),
                                                profile_key, clean=stage.get("args", {}).get("clean") is True)
                    raw_adapter.bind_incremental(plan, lease.unity_project_resource(project_dir))
                low = check_disk(name)
                if low is not None:
                    return low
                ctx.set_phase("building")
                # Build output goes to the work root (outside protected folders, removed with the job) when the job has one.
                out = ctx.work.build if ctx.work is not None else directory / "build"
                artifacts.register_temp(ctx.layout, out)
                adapter.bind_temporary_registrar(lambda path: artifacts.register_temp(ctx.layout, path))
                adapter.bind_build_budget(float(stage.get("timeoutSec") or BUILD_STAGE_DEFAULT_SEC))
                build_outcome = _bounded(lambda: adapter.build(project_dir, profile, out), stage, adapter_name="build",
                                         default_sec=BUILD_STAGE_DEFAULT_SEC)
                lost = [resource for resource in ctx.lost_leases() if resource not in lost_before]
            finally:
                # A build that timed out may still be running in its thread or process: its leases stay until the job ends.
                if not (build_outcome is not None and build_outcome.status == constants.TIMEOUT):
                    for item in reversed(taken):
                        ctx.release(item)
                ctx.set_phase(None)
            for key, value in build_outcome.details.items():
                if key.startswith("temp."):
                    artifacts.register_temp(ctx.layout, Path(value))
            if build_outcome.details.get("incremental.note"):
                head_note.append("incremental build: " + str(build_outcome.details["incremental.note"]))
                events.emit("log", "incremental build: " + str(build_outcome.details["incremental.note"]), step=name)
            extra_layers.update(_stage_outcome_layers("build", build_outcome))
            if build_outcome.ok and build_outcome.artifacts:
                artifact = build_outcome.artifacts[0]
            events.emit("step.finished", build_outcome.reason, step=name, status=build_outcome.status)
            if build_outcome.ok and lost:
                # The build ran without the capacity it reserved: its result cannot be trusted to have been exclusive.
                reason = "a lease was lost while the build ran: " + ", ".join(lost)
                stage_results.append(early(constants.BLOCKED, reason))
                return fail(constants.BLOCKED, reason)
            if not build_outcome.ok:
                stage_results.append(early(build_outcome.status, f"build failed: {build_outcome.reason}"))
                return fail(build_outcome.status, f"build failed: {build_outcome.reason}")
            if artifact is not None and cache_key is not None:
                try:
                    published = buildcache.publish(
                        buildcache.builds_root(ctx.home), cache_key, artifact, out=out, project=pipeline["candidate"]["repo"], platform=platform,
                        sha=candidate_sha, profile=profile, dirty=dirty, toolchain=toolchain, job_id=ctx.job_id,
                        cacheable=not cache_key.startswith("dirty-"), replace=stage.get("args", {}).get("reuse") is False,
                        source=_build_source(pipeline["candidate"]["repo"], options.checkout_id, project_dir),
                        config_hash=config_hash_seen[0] or buildcache.profile_config_hash(project_dir, profile))
                except (OSError, ValueError) as error:
                    head_note.append(f"the build artifact could not be published outside the work folder: {error}")
                else:
                    artifact, used_entry = published.path, published
                    build_info = {**published.describe(), "reused": False, "builtByJob": ctx.job_id}
                    diskspace.remember_build(directory, published.key)
                    events.emit("progress", f"build published {published.key}", step=name, data={"build": build_info})
                    _collect_build_cache(ctx, published.key)
                    release_build_output()  # also the leftovers of an earlier unpublished build: the registry lists them all
                    continue
            build_pending_release = True  # not published (no key, or it failed): the output is released after the install
            continue
        if kind in ("install", "run"):
            blocked = claim_device(name)
            if blocked is not None:
                return blocked
        if kind == "install":
            if build_outcome is not None and not build_outcome.ok:
                return fail(constants.BLOCKED, "install refused: the build did not pass")  # defensive; the build failure returned above
            args = stage.get("args", {})
            chosen = artifact
            if chosen is None and args.get("artifactKey"):
                entry = buildcache.find(buildcache.builds_root(ctx.home), str(args["artifactKey"]))
                if entry is None:
                    events.emit("step.finished", "unknown build", step=name, status=constants.BLOCKED)
                    return fail(constants.BLOCKED, f"install: no published build with key {args['artifactKey']} (see `valtrion builds list`)")
                if not buildcache.is_intact(entry):
                    events.emit("step.finished", "damaged build", step=name, status=constants.BLOCKED)
                    return fail(constants.BLOCKED, f"install: the published build {entry.key} is damaged (its files differ in size from the record)")
                if entry.manifest["platform"] != platform:
                    events.emit("step.finished", "wrong platform", step=name, status=constants.BLOCKED)
                    return fail(constants.BLOCKED, f"install: the published build {entry.key} is for {entry.manifest['platform']}, not {platform}")
                buildcache.touch(entry)
                used_entry = entry
                chosen = entry.path
                build_info = {**entry.describe(), "reused": True, "builtByJob": entry.manifest.get("builtByJob", "unknown")}
                diskspace.remember_build(directory, entry.key)
                head_note.append(f"installed the published build {entry.key} (candidate {entry.manifest['sha'][:12]}, "
                                 f"{'dirty' if entry.manifest['dirty'] else 'clean'}); this job did not build it")
            if chosen is None and args.get("artifact"):
                chosen = Path(args["artifact"])
            if chosen is None:
                events.emit("step.finished", "no artifact", step=name, status=constants.BLOCKED)
                return fail(constants.BLOCKED, "install needs the artifact of a build stage, stage.args.artifactKey or stage.args.artifact")
            if used_entry is not None:
                buildcache.touch(used_entry)
            installed = _bounded(lambda: adapter.install(device, chosen), stage, adapter_name="install")
            extra_layers["install"] = installed.status
            events.emit("step.finished", installed.reason, step=name, status=installed.status)
            if not installed.ok:
                stage_results.append(early(installed.status, f"install failed: {installed.reason}"))
                return fail(installed.status, f"install failed: {installed.reason}")
            continue
        if kind == "run":
            assert device is not None
            if enforce_checkout and not adapter.build_uses_editor and project_dir is not None:
                # The player runs from the folder itself (the Editor platform): another job may have moved it since this job's build stage.
                moved = checkout.verify_head(project_dir, candidate_sha, options.git)
                if moved:
                    events.emit("step.finished", moved, step=name, status=constants.BLOCKED)
                    stage_results.append(early(constants.BLOCKED, moved))
                    return fail(constants.BLOCKED, moved)
            if not stage.get("plan"):
                return fail(constants.BLOCKED, "a run stage needs a plan")
            plan_path, plan, plan_sha = load_plan(stage["plan"])
            repeats = int(stage.get("repeat", 1))
            runs: list[dict] = []
            for attempt in range(1, repeats + 1):
                run_id = core.safe_identifier(f"{ctx.job_id}-{index + 1}-{attempt}")
                run_directory = directory / "runs" / f"{index + 1:02d}-{core.safe_segment(plan['id'], 'plan', 40)}-{attempt}"
                outcome = run_on_device(adapter, device, plan_path, plan, stage, run_directory, options, ctx.cancelled)
                document = assemble_result(run_id, outcome, pipeline, plan, plan_sha, device, host, unity, dirty, extra_layers, head_note,
                                           build_info, checkout_info)
                runs.append(finalize_result(document, run_directory, run_directory / "result.json", include_top=True))
                events.emit("progress", f"run {attempt}/{repeats} {outcome.status}", step=name, data={"run": attempt, "status": outcome.status})
                ctx.flush()
                if outcome.status != constants.PASS and not plan.get("continueOnFail"):
                    break
            merged = _merge_runs(runs, plan, plan_sha)
            events.emit("step.finished", merged["reason"], step=name, status=merged["status"])
            stage_results.append(merged)
            series_plans[merged["runId"]] = plan
            if merged["status"] != constants.PASS:
                break
            continue
        if kind == "collect":
            events.emit("step.finished", "device output is collected by each run stage", step=name, status=constants.PASS)
            continue
    return conclude()


def _bounded(call: Callable[[], StepOutcome], stage: Mapping[str, Any], adapter_name: str, default_sec: float = 3600.0) -> StepOutcome:
    timeout = float(stage.get("timeoutSec") or default_sec)
    try:
        finished, outcome = supervise.call_bounded(call, timeout)
    except Exception as error:  # noqa: BLE001 - adapters report expected conditions as outcomes; an exception is a bug to surface
        return StepOutcome(constants.FAIL, f"{adapter_name} raised {type(error).__name__}: {error}")
    if not finished:
        return StepOutcome(constants.TIMEOUT, f"{adapter_name} did not return within {timeout:g} s")
    return outcome


_OK_VERDICTS = ("PASS", "IMPROVED")


def _worst_result(results: Sequence[dict]) -> dict:
    """The result of the job: the first non-passing one, else the first with the worst comparison verdict, else the last."""
    failing = next((r for r in results if r["status"] != constants.PASS), None)
    if failing is not None:
        return failing
    compared = [r for r in results if r.get("comparison")]
    if not compared:
        return results[-1]
    worst = compare.worst_verdict([r["comparison"]["verdict"] for r in compared])
    return next(r for r in compared if r["comparison"]["verdict"] == worst)


def _with_other_series(chosen: dict, results: Sequence[dict]) -> dict:
    """The job result, with the verdicts of the series that are not ok added to its comparison so that it does not read as the
    whole story. Returns a copy; the series results stay as they were."""
    comparison = chosen.get("comparison")
    others = [r for r in results if r is not chosen and r.get("comparison") and r["comparison"]["verdict"] not in _OK_VERDICTS]
    if not comparison or not others:
        return chosen
    notes = [f"series {r['plan']['id']}: {r['comparison']['verdict']}" for r in others]
    return {**chosen, "comparison": {**comparison, "details": [*comparison["details"], *notes]}}


def _summary_entry(result: Mapping[str, Any]) -> dict:
    entry = {"runId": result["runId"], "plan": result["plan"]["id"], "status": result["status"]}
    if result.get("comparison"):
        entry["comparison"] = dict(result["comparison"])
    return entry


def _job_status(chosen: Mapping[str, Any], results: Sequence[Mapping[str, Any]] = ()) -> tuple[str, str | None]:
    """The job status: the failure of the job result, else the worst comparison verdict of all series (REGRESSION is a FAIL,
    NOT_COMPARABLE and NOISY are BLOCKED, never a PASS)."""
    if chosen["status"] != constants.PASS:
        regressed = [r["plan"]["id"] for r in results if r is not chosen and (r.get("comparison") or {}).get("verdict") == "REGRESSION"]
        note = f" (an earlier series also regressed: {', '.join(regressed)})" if regressed else ""
        return chosen["status"], f"{chosen['reason']}{note}" if note else chosen["reason"]
    compared = [r for r in (results or [chosen]) if r.get("comparison")]
    problems = [r for r in compared if r["comparison"]["verdict"] not in _OK_VERDICTS]
    if not problems:
        return constants.PASS, None
    verdict = compare.worst_verdict([r["comparison"]["verdict"] for r in problems])
    lines = [f"{r['plan']['id']} {r['comparison']['verdict']}: " + "; ".join(r["comparison"]["details"][:2]) for r in problems]
    return (constants.FAIL if verdict == "REGRESSION" else constants.BLOCKED), f"the run passed but the comparison is {verdict}: " + " | ".join(lines)


def _merge_runs(runs: list[dict], plan: Mapping[str, Any], plan_sha: str) -> dict:
    """One result for the repeats of a stage: the first non-passing run, else the first run with all samples joined. The warmup
    samples of every run after the first are left out, so that dropping the plan's warmup from the joined samples (as the
    comparison and a baseline do) removes the warmup of each run and no more. A run with fewer samples than the warmup has
    nothing but warmup: it is left out and the next run leads. As in a single run, `stats` describe the samples as joined
    (the leading run's warmup included); the comparison and a baseline compute their own statistics after the warmup."""
    failing = next((r for r in runs if r["status"] != constants.PASS), None)
    base = dict(failing or runs[0])
    if failing is None and len(runs) > 1:
        warmup = max(0, int(plan.get("warmup", 0)))
        names = {m["name"] for r in runs for m in r["metrics"]}
        merged = []
        left_out: list[str] = []
        for name in sorted(names):
            parts = [m for r in runs for m in r["metrics"] if m["name"] == name]
            usable = [m for m in parts if len(m["samples"]) >= warmup]
            if len(usable) < len(parts):
                left_out.append(name)
            usable = usable or parts[:1]
            samples = [*usable[0]["samples"], *(s for m in usable[1:] for s in m["samples"][warmup:])]
            merged.append({"name": name, "unit": parts[0]["unit"], "samples": samples, "stats": compare.compute_stats(samples)})
        base["metrics"] = merged
        base["unverified"] = sorted({u for r in runs for u in r["unverified"]} | {
            f"{name}: a repeat had fewer samples than the plan's warmup and was left out of the joined samples" for name in left_out})
    return base


def _build_source(project: str, checkout_id: str | None, project_dir: Path) -> str:
    """Where a published build came from, for the `buildCache.perSource` limit: the managed checkout, or else the project folder."""
    if checkout_id is not None:
        return f"checkout:{project}/{checkout_id}"
    return "dir:" + os.path.normcase(str(project_dir.resolve()))


def _collect_build_cache(ctx: JobContext, keep_key: str) -> None:
    """Retention of the published builds, applied after each publish (the new build is never removed by it)."""
    try:
        limits = config.load_host_settings(ctx.home).build_cache.limits()
        report = buildcache.gc(buildcache.builds_root(ctx.home), protect=[keep_key, *diskspace.active_build_keys(ctx.home)], **limits)
    except (OSError, ValueError, config.ConfigError) as error:
        ctx.events.emit("log", f"build cache: retention skipped: {type(error).__name__}: {error}")
        return
    if report.removed:
        ctx.events.emit("log", "build cache: removed " + ", ".join(report.removed), data={"removed": list(report.removed)})


def _early_result(ctx, pipeline, plans, load_plan, first_plan, device, host, unity, dirty, extra_layers, status, reason,
                  head_note: Sequence[str] = (), build: Mapping[str, Any] | None = None,
                  checkout: Mapping[str, Any] | None = None) -> dict:
    """The result of a pipeline that stopped before any run (a failed build, no device) or has no run stage (build only,
    install only): no metrics, a clear reason."""
    plan_id, plan_sha = "unknown", core.sha256_bytes(b"")
    if first_plan:
        try:
            _, plan, plan_sha = load_plan(first_plan)
            plan_id = plan["id"]
        except (OSError, ValueError):
            pass
    stamp = core.utc_now()
    outcome = DeviceRun(status, reason, None, {}, ctx.store.directory, stamp, stamp, [])
    return assemble_result(core.safe_identifier(ctx.job_id), outcome, pipeline, {"id": plan_id, "metrics": {}}, plan_sha, device, host,
                           unity, dirty, extra_layers, ["no run was started on a device", *head_note], build, checkout)


def baseline_folders(baselines: str, pipeline_dir: Path, project_dir: Path | None) -> list[Path]:
    """The baselines folders a pipeline's `compare` names. An absolute path is itself; a relative one is looked for under the
    pipeline file's folder (where its plans resolve) and under the project folder, both searched, never under the working
    directory, so the answer does not depend on where the command was started."""
    configured = Path(baselines)
    if configured.is_absolute():
        return [configured]
    roots = [pipeline_dir, *([project_dir] if project_dir is not None else [])]
    folders: list[Path] = []
    for root in roots:
        folder = root / configured
        if folder not in folders:
            folders.append(folder)
    return folders


def _attach_comparison(result: dict, plan: Mapping[str, Any], folders: Sequence[Path]) -> None:
    """Compare one series result with the baseline of its own plan and key and record the verdict in the result."""
    comparison, shown = compare.compare_against_folders(result, plan, folders)
    result["comparison"] = {"baseline": shown, "verdict": comparison.verdict, "details": comparison.details or ["no details"]}


def run_pipeline_job(ctx: JobContext) -> PipelineOutcome:
    """Payload of a pipeline job: read the pipeline file, validate it, execute it."""
    payload = ctx.spec["payload"]
    path = Path(payload["file"])
    if "inline" in payload:
        pipeline = payload["inline"]  # `valtrion run` builds a one-run pipeline in memory
    else:
        try:
            pipeline = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            return PipelineOutcome(constants.BLOCKED, f"cannot read the pipeline {path}: {error}")
    _, errors = validate_document(pipeline)
    if errors:
        return PipelineOutcome(constants.FAIL, f"{path} is not a valid pipeline: " + "; ".join(errors[:5]))
    options = PipelineOptions(
        project_dir=Path(payload["projectDir"]) if payload.get("projectDir") else None,
        pipeline_dir=Path(payload["baseDir"]) if payload.get("baseDir") else path.parent,
        device_poll_sec=float(payload.get("devicePollSec", DEFAULT_DEVICE_POLL_SEC)),
        op_timeout_sec=float(payload.get("opTimeoutSec", 300.0)), sha_pinned=bool(payload.get("shaPinned", False)),
        checkout_id=payload.get("checkoutId"), checkout_root=Path(payload["checkoutRoot"]) if payload.get("checkoutRoot") else None,
        checkout_remote=payload.get("checkoutRemote"), unity_path=payload.get("unityPath") or ".")
    return execute(pipeline, path, ctx, options)
