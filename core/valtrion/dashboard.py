"""What the dashboard shows: one read-only snapshot of this machine, as plain JSON.

Leases (who holds the project, the Editor slots and the devices, who waits), jobs (the running ones with their steps and phase, the recent
ones with their status), Unity Editors that run now, free memory and disk. Nothing here changes anything: it reads the files Valtrion keeps
under its home and asks the system for the process list and the free memory."""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import core, jobs, lease, testprogress, unityguard

SCHEMA = "gdv.dashboard/1"
RECENT_JOBS = 30
EVENT_TAIL_BYTES = 512 * 1024
COMMAND_LIMIT = 300
_PROJECT_PATH = re.compile(r'-projectPath\s+("([^"]+)"|(\S+))', re.IGNORECASE)


def _read(path: Path) -> dict | None:
    try:
        document = core.read_json(path)
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _age_sec(utc: str | None, now: datetime) -> float | None:
    if not utc:
        return None
    try:
        return max(0.0, round((now - core.parse_utc(utc)).total_seconds(), 1))
    except (ValueError, TypeError):
        return None


# -- leases ---------------------------------------------------------------------------------------------------------------------


def leases(home: Path, now: datetime) -> list[dict]:
    manager = lease.LeaseManager(home)
    waiting: dict[str, int] = {}
    queue = manager.directory / lease.QUEUE_FOLDER
    if queue.is_dir():
        for folder in queue.iterdir():
            if folder.is_dir():
                waiting[folder.name.replace("%3A", ":")] = sum(1 for _ in folder.glob("*.ticket"))
    result = []
    for status in manager.list_all():
        entry: dict[str, Any] = {"resource": status.resource, "state": status.state, "waiting": waiting.pop(status.resource, 0)}
        record = status.record
        if record is not None:
            entry.update({
                "owner": record.owner, "session": record.session, "pid": record.pid, "host": record.host, "purpose": record.purpose,
                "worktree": record.worktree, "acquiredUtc": record.acquired_utc, "heldSec": _age_sec(record.acquired_utc, now),
                "renewedSec": _age_sec(record.renewed_utc, now), "ttlSec": record.ttl_sec})
        result.append(entry)
    for resource, count in sorted(waiting.items()):  # waiters of a resource nobody holds right now
        if count:
            result.append({"resource": resource, "state": "free", "waiting": count})
    return result


# -- jobs -----------------------------------------------------------------------------------------------------------------------


def _event_lines(path: Path) -> list[dict]:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - EVENT_TAIL_BYTES))
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").split("\n")
    if size > EVENT_TAIL_BYTES:
        lines = lines[1:]  # the first piece of a cut is not a whole line
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _progress(directory: Path) -> dict:
    """Steps (in order, with their status), the current phase and the newest message of a job, from its event log."""
    steps: dict[str, str] = {}
    phase = None
    message = None
    elapsed = None
    for event in _event_lines(directory / "events.jsonl"):
        kind = event.get("type")
        step = event.get("step")
        if kind == "step.started" and step:
            steps[step] = "running"
        elif kind == "step.finished" and step:
            steps[step] = str(event.get("status") or "done")
        elif kind == "progress":
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            if data.get("phase"):
                phase = str(data["phase"])
            if data.get("elapsedSec") is not None:
                elapsed = data["elapsedSec"]
            message = event.get("message") or message
    current = next((name for name, state in reversed(list(steps.items())) if state == "running"), None)
    return {"steps": [{"step": name, "status": state} for name, state in steps.items()], "currentStep": current, "phase": phase,
            "message": message, "elapsedSec": elapsed}


def job_list(home: Path, now: datetime, limit: int = RECENT_JOBS) -> list[dict]:
    folder = home / "jobs"
    if not folder.is_dir():
        return []
    entries = []
    for entry in folder.glob("*.json"):
        try:
            entries.append((entry.stat().st_mtime, entry))
        except OSError:
            continue
    result = []
    for _, entry in sorted(entries, key=lambda item: item[0], reverse=True)[:limit]:
        pointer = _read(entry)
        if pointer is None or "dir" not in pointer:
            continue
        directory = Path(pointer["dir"])
        job = _read(directory / "job.json")
        if job is None:
            continue
        running = job.get("state") in ("queued", "running")
        alive = False
        if running:
            try:
                alive = jobs._runner_is_ours(jobs.JobStore(directory), job.get("pid"))  # noqa: SLF001 - the same check the sweeper uses
            except (OSError, ValueError, KeyError):
                alive = False
        item: dict[str, Any] = {
            "id": job.get("jobId") or entry.stem, "kind": job.get("kind"), "state": job.get("state"), "status": job.get("status"),
            "reason": job.get("reason"), "createdUtc": job.get("createdUtc"), "updatedUtc": job.get("updatedUtc"),
            "ageSec": _age_sec(job.get("createdUtc"), now), "active": bool(running and alive), "host": job.get("host"),
            "project": directory.parent.parent.name if directory.parent.parent != directory.parent else "",
            "stallSec": (job.get("watchdog") or {}).get("stallSec"), "timeoutSec": (job.get("watchdog") or {}).get("timeoutSec"),
            "lastProgressSec": _age_sec((job.get("watchdog") or {}).get("lastProgressUtc"), now)}
        item.update(_progress(directory))
        spec = _read(directory / "spec.json") or {}
        item["worktree"] = core.worktree_label((spec.get("payload") or {}).get("projectDir") if isinstance(spec.get("payload"), dict) else None)
        if item["active"]:
            item["device"] = device_progress(directory, item["currentStep"], now)
        result.append(item)
    return result


# -- Unity Editors, memory, disk --------------------------------------------------------------------------------------------------


def unity_editors(probe: unityguard.Probe) -> dict:
    processes = probe.processes()
    if processes is None:
        return {"readable": False, "editors": []}
    editors = []
    for proc in processes:
        if not unityguard.is_unity_editor(proc, probe.os_name):
            continue
        command = proc.command or proc.path
        match = _PROJECT_PATH.search(command)
        project = (match.group(2) or match.group(3)) if match else ""
        editors.append({
            "pid": proc.pid, "ppid": proc.ppid, "project": project, "worker": "AssetImportWorker" in command or "-adb2" in command,
            "batch": "-batchmode" in command.lower(), "tests": "-runtests" in command.lower(), "command": command[:COMMAND_LIMIT]})
    editors.sort(key=lambda editor: (editor["worker"], editor["pid"]))
    return {"readable": True, "editors": editors}


MEMORY_GROUPS = 8
_WINDOWS_MEMORY = (
    "$os = Get-CimInstance Win32_OperatingSystem; "
    "[pscustomobject]@{total=$os.TotalVisibleMemorySize; free=$os.FreePhysicalMemory; "
    "procs=@(Get-Process | ForEach-Object { [pscustomobject]@{id=$_.Id; name=$_.ProcessName; ws=$_.WorkingSet64} })} "
    "| ConvertTo-Json -Compress -Depth 4")


def parse_windows_memory(text: str | None) -> dict | None:
    """Total and free memory (MiB) and the working set of every process, from the PowerShell command above."""
    if not text:
        return None
    try:
        document = json.loads(text[text.index("{"):])
        processes = [{"pid": int(item["id"]), "name": str(item["name"]), "mib": int(item["ws"]) / 2 ** 20}
                     for item in document["procs"] if isinstance(item, dict)]
        return {"totalMiB": int(document["total"]) / 1024, "freeMiB": int(document["free"]) / 1024, "processes": processes}
    except (ValueError, KeyError, TypeError):
        return None


def parse_ps_memory(ps_text: str | None, total_mib: float | None, free_mib: float | None) -> dict | None:
    """Resident memory of every process from `ps -axo pid=,rss=,comm=` (rss in KiB, comm a path on macOS)."""
    if not ps_text or total_mib is None:
        return None
    processes = []
    for line in ps_text.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            processes.append({"pid": int(parts[0]), "name": parts[2].replace("\\", "/").rsplit("/", 1)[-1], "mib": int(parts[1]) / 1024})
    return {"totalMiB": total_mib, "freeMiB": free_mib, "processes": processes} if processes else None


class DashboardProbe(unityguard.SystemProbe):
    """The system probe of the Unity guard, plus how much memory the machine has and what every process holds."""

    def memory_overview(self) -> dict | None:
        if self.os_name == "windows":
            return parse_windows_memory(self._powershell(_WINDOWS_MEMORY))
        ps_text = self._run(["ps", "-axo", "pid=,rss=,comm="], self.timeout_sec)
        total = None
        if self.os_name == "macos":
            raw = self._run(["sysctl", "-n", "hw.memsize"], self.timeout_sec)
            total = int(raw.strip()) / 2 ** 20 if raw and raw.strip().isdigit() else None
        else:
            try:
                match = re.search(r"^MemTotal:\s+(\d+)\s*kB", self._meminfo.read_text(encoding="utf-8"), re.MULTILINE)
                total = int(match.group(1)) / 1024 if match else None
            except OSError:
                total = None
        return parse_ps_memory(ps_text, total, self.free_memory_mib())


def memory_groups(processes: list[dict], unity_labels: dict[int, str], limit: int = MEMORY_GROUPS) -> list[dict]:
    """What holds the memory: processes of one program together, every Unity process under the project it works on."""
    groups: dict[str, dict] = {}
    for item in processes:
        label = unity_labels.get(item["pid"])
        name = label or re.sub(r"\.exe$", "", item["name"], flags=re.IGNORECASE)
        entry = groups.setdefault(name, {"label": name, "mib": 0.0, "count": 0, "unity": label is not None})
        entry["mib"] += item["mib"]
        entry["count"] += 1
    ordered = sorted(groups.values(), key=lambda entry: entry["mib"], reverse=True)
    top, rest = ordered[:limit], ordered[limit:]
    result = [{**entry, "mib": round(entry["mib"])} for entry in top]
    if rest:
        result.append({"label": f"{len(rest)} other programs", "mib": round(sum(entry["mib"] for entry in rest)),
                       "count": sum(entry["count"] for entry in rest), "unity": False, "other": True})
    return result


def unity_labels(editors: dict) -> dict[int, str]:
    labels = {}
    for editor in editors.get("editors", []):
        name = Path(editor["project"].replace("\\", "/")).name if editor.get("project") else ""
        labels[editor["pid"]] = f"Unity · {name}" if name else "Unity Editor"
    return labels


def machine(home: Path, probe: unityguard.Probe, editors: dict | None = None) -> dict:
    result: dict[str, Any] = {"freeMemoryMiB": None, "diskFreeGiB": None, "diskTotalGiB": None, "memory": None}
    try:
        free = probe.free_memory_mib()
        result["freeMemoryMiB"] = None if free is None else round(free)
    except Exception:  # noqa: BLE001 - a number that cannot be read is a gap in the picture, not an error
        pass
    reader = getattr(probe, "memory_overview", None)
    if reader is not None:
        try:
            overview = reader()
        except Exception:  # noqa: BLE001 - same
            overview = None
        if overview:
            total, free_mib = overview["totalMiB"], overview.get("freeMiB")
            result["memory"] = {
                "totalMiB": round(total), "freeMiB": None if free_mib is None else round(free_mib),
                "usedMiB": None if free_mib is None else round(total - free_mib),
                "groups": memory_groups(overview["processes"], unity_labels(editors or {}))}
            if result["freeMemoryMiB"] is None and free_mib is not None:
                result["freeMemoryMiB"] = round(free_mib)
    try:
        usage = shutil.disk_usage(home if home.exists() else home.anchor or ".")
        result["diskFreeGiB"] = round(usage.free / 2 ** 30, 1)
        result["diskTotalGiB"] = round(usage.total / 2 ** 30, 1)
    except OSError:
        pass
    return result


class Collector:
    """Builds snapshots. The process list is the one costly part (a PowerShell call on Windows), so it is taken at most every
    `process_ttl_sec`; the rest is read at every call."""

    def __init__(self, home: Path | None = None, probe: unityguard.Probe | None = None, process_ttl_sec: float = 6.0,
                 now: Callable[[], datetime] = core.now, clock: Callable[[], float] = time.monotonic, background: bool = False) -> None:
        """`background`: an out-of-date process list is shown as it is while a new one is read in a thread (a server wants its answers
        at once); otherwise the caller waits for the new list."""
        self.home = home or core.valtrion_home()
        self._probe = probe
        self._ttl = process_ttl_sec
        self._now = now
        self._clock = clock
        self._background = background
        self._cached: tuple[float, dict, dict] | None = None
        self._refreshing = False
        self._lock = threading.Lock()

    def _refresh(self) -> tuple[dict, dict]:
        try:
            probe = self._probe or DashboardProbe()
            editors = unity_editors(probe)
            stats = machine(self.home, probe, editors)
            self._cached = (self._clock(), editors, stats)
            return editors, stats
        finally:
            with self._lock:
                self._refreshing = False

    def _system(self) -> tuple[dict, dict]:
        cached = self._cached
        if cached is not None and self._clock() - cached[0] < self._ttl:
            return cached[1], cached[2]
        if self._background and cached is not None:
            with self._lock:
                start = not self._refreshing
                self._refreshing = True
            if start:
                threading.Thread(target=self._refresh, name="valtrion-dashboard-processes", daemon=True).start()
            return cached[1], cached[2]
        return self._refresh()

    def snapshot(self) -> dict:
        now = self._now()
        editors, stats = self._system()
        held = leases(self.home, now)
        return {
            "schema": SCHEMA, "host": {"id": core.local_host_id(), "os": core.local_os()}, "utc": core.format_utc(now),
            "leases": held, "jobs": job_list(self.home, now), "unity": editors, "machine": stats, "devices": devices(self.home, held),
            "slots": slots(self.home, held, now)}


def slots(home: Path, held: list[dict], now: datetime | None = None) -> dict:
    """The Editor slots of this host, all of them, each free or held (with what runs in it): the same total that `unity run` counts."""
    try:
        capacity = lease.unity_editor_capacity(home)
    except (ValueError, OSError):
        capacity = lease.DEFAULT_UNITY_EDITOR_SLOTS
    by_resource = {entry["resource"]: entry for entry in held if entry.get("state") == "held"}
    projects = {entry["session"]: entry.get("worktree") or entry.get("purpose") for entry in held
                if entry.get("resource", "").startswith("unity-project:") and entry.get("state") == "held"}
    editor_slots = []
    for number in range(1, max(capacity, 1) + 1):
        entry = by_resource.get(lease.slot_resource(lease.UNITY_EDITOR_RESOURCE, number))
        if entry is None:
            editor_slots.append({"slot": number, "state": "free"})
            continue
        project = projects.get(entry.get("session"))
        slot = {
            "slot": number, "state": "held", "purpose": entry.get("purpose"), "owner": entry.get("owner"), "pid": entry.get("pid"),
            "heldSec": entry.get("heldSec"), "renewedSec": entry.get("renewedSec"), "project": project,
            "worktree": core.worktree_label(project)}
        counted = testprogress.read(testprogress.path_for(entry["session"], home)) if entry.get("session") else None
        if counted is not None:  # the Unity package reports "N of M tests" for this run
            counted["updatedSec"] = _age_sec(counted["utc"], now or core.now())  # the Editor process wrote the file (a background thread)
            counted["mainSec"] = _age_sec(counted["mainUtc"], now or core.now())  # the Editor's main thread last ran (a long test blocks it)
            slot["progress"] = counted
        editor_slots.append(slot)
    return {"capacity": capacity, "unityEditor": editor_slots}


def _plan_step_count(spec: dict, current_step: str | None) -> int | None:
    """How many steps the plan of the run stage that runs now has (the plan file the pipeline names, on this host)."""
    payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
    inline = payload.get("inline") if isinstance(payload.get("inline"), dict) else {}
    stages = [stage for stage in inline.get("stages", []) if isinstance(stage, dict)]
    chosen = None
    match = re.match(r"^(\d+):", current_step or "")
    if match and 0 < int(match.group(1)) <= len(stages) and stages[int(match.group(1)) - 1].get("plan"):
        chosen = stages[int(match.group(1)) - 1]
    else:
        planned = [stage for stage in stages if stage.get("plan")]
        chosen = planned[0] if len(planned) == 1 else None
    if chosen is None:
        return None
    plan_path = Path(str(chosen["plan"]))
    if not plan_path.is_absolute() and payload.get("baseDir"):
        plan_path = Path(str(payload["baseDir"])) / plan_path
    plan = _read(plan_path)
    steps = plan.get("steps") if plan else None
    return len(steps) if isinstance(steps, list) and steps else None


def device_progress(directory: Path, current_step: str | None, now: datetime) -> dict | None:
    """Where the app on a device is in its plan, from the heartbeat the harness writes (pulled into the run folder while it runs)."""
    newest = None
    for path in (directory / "runs").rglob("heartbeat.json") if (directory / "runs").is_dir() else []:
        try:
            moment = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or moment > newest[0]:
            newest = (moment, path)
    if newest is None:
        return None
    beat = _read(newest[1])
    if not beat or beat.get("schema") != "gdv.heartbeat/1" or not isinstance(beat.get("stepIndex"), int):
        return None
    total = _plan_step_count(_read(directory / "spec.json") or {}, current_step)
    index = beat["stepIndex"]
    step = total if beat.get("state") == "done" and total else (0 if index < 0 else index + 1)
    return {"step": step, "total": total, "stepType": beat.get("stepType") or "", "state": beat.get("state"),
            "message": beat.get("message") or "", "ageSec": _age_sec(beat.get("utc"), now)}


# -- devices ----------------------------------------------------------------------------------------------------------------------


def devices(home: Path, held: list[dict]) -> list[dict]:
    """Devices of this host's registry (phones, tablets, emulators, the Editor) with who uses them right now, from the device leases."""
    registry = _read(home / "registry.json")
    if registry is None or not isinstance(registry.get("devices"), list):
        return []
    leased = {entry["resource"]: entry for entry in held if entry.get("state") == "held"}
    result = []
    for device in registry["devices"]:
        if not isinstance(device, dict) or "id" not in device:
            continue
        holder = leased.get(f"device:{device['id']}")
        result.append({
            "id": device["id"], "platform": device.get("platform"), "model": device.get("model") or device.get("class"),
            "os": device.get("os"), "kind": device.get("evidence"), "online": bool(device.get("online", True)),
            "exclusive": bool(device.get("exclusive", True)),
            "busy": holder is not None, "purpose": holder.get("purpose") if holder else None,
            "heldSec": holder.get("heldSec") if holder else None})
    return result


# -- other hosts ------------------------------------------------------------------------------------------------------------------

REMOTE_TIMEOUT_SEC = 45
REMOTE_REFRESH_SEC = 8.0
_OLD_CLI = ("unrecognized arguments", "invalid choice", "unknown command")


def fetch_remote(entry, call=None) -> dict:
    """Ask one SSH host for its snapshot (`valtrion dashboard --snapshot` there). Never raises: the answer says what happened.
    A host whose Valtrion has no `dashboard` command yet is reported as `legacy` (it needs `valtrion hosts sync`)."""
    from . import remote

    base = {"id": entry.id, "os": entry.os}
    try:
        if call is None:
            session = remote.remote_for(entry, None, transport=remote.make_transport(entry, retry=False))
            call = lambda args: session.call(args, REMOTE_TIMEOUT_SEC)  # noqa: E731
        result = call(["dashboard", "--snapshot"])
    except (OSError, ValueError) as error:
        return {**base, "ok": False, "error": str(error)[:300]}
    if result.ok:
        document = remote.extract_json_object(result.stdout)
        if isinstance(document, dict) and document.get("schema") == SCHEMA:
            return {**base, "ok": True, "snapshot": document}
        return {**base, "ok": False, "error": "the host answered, but not with a dashboard snapshot"}
    text = f"{result.stderr}\n{result.stdout}".lower()
    if any(marker in text for marker in _OLD_CLI):
        return {**base, "ok": True, "legacy": True,
                "note": "Valtrion on this host is older than the dashboard: run `valtrion hosts sync` to see its jobs and leases here."}
    if result.timed_out:
        return {**base, "ok": False, "error": f"no answer within {REMOTE_TIMEOUT_SEC} s"}
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    return {**base, "ok": False, "error": (detail[-1] if detail else f"exit code {result.returncode}")[:300]}


class Fleet:
    """This machine and the SSH hosts of hosts.json. This machine is read at every call; each other host is asked in the background at
    most every `refresh_sec` (an SSH round trip is slow), and the page shows the last answer with its age until the next one arrives."""

    def __init__(self, home: Path | None = None, local: Collector | None = None, hosts=None, fetch=fetch_remote,
                 refresh_sec: float = REMOTE_REFRESH_SEC, clock: Callable[[], float] = time.monotonic, background: bool = True) -> None:
        from . import config

        self.home = home or core.valtrion_home()
        self.local = local or Collector(self.home, background=True)
        self._hosts = hosts or (lambda: [entry for entry in config.load_hosts(self.home) if entry.kind != "local"])
        self._fetch = fetch
        self._refresh = refresh_sec
        self._clock = clock
        self._background = background
        self._cache: dict[str, tuple[float, dict]] = {}
        self._running: set[str] = set()
        self._lock = threading.Lock()

    def _ask(self, entry) -> None:
        try:
            answer = self._fetch(entry)
        except Exception as error:  # noqa: BLE001 - one host's trouble must not take the page down
            answer = {"id": entry.id, "os": entry.os, "ok": False, "error": f"{type(error).__name__}: {error}"[:300]}
        with self._lock:
            self._cache[entry.id] = (self._clock(), answer)
            self._running.discard(entry.id)

    def _remote(self, entry) -> dict:
        with self._lock:
            cached = self._cache.get(entry.id)
            stale = cached is None or self._clock() - cached[0] >= self._refresh
            start = stale and entry.id not in self._running
            if start:
                self._running.add(entry.id)
        if start:
            if self._background:
                threading.Thread(target=self._ask, args=(entry,), name=f"valtrion-dashboard-{entry.id}", daemon=True).start()
            else:
                self._ask(entry)
                cached = self._cache.get(entry.id)
        if cached is None:
            with self._lock:
                cached = self._cache.get(entry.id)
        if cached is None:
            return {"id": entry.id, "os": entry.os, "ok": None, "loading": True}
        return {**cached[1], "ageSec": round(self._clock() - cached[0], 1)}

    def snapshot(self) -> dict:
        document = self.local.snapshot()
        try:
            entries = list(self._hosts())
        except Exception as error:  # noqa: BLE001 - a hosts.json that cannot be read leaves this machine's picture intact
            document["remotes"] = []
            document["remotesError"] = str(error)[:300]
            return document
        document["remotes"] = [self._remote(entry) for entry in entries]
        return document
