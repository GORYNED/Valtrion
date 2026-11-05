"""Handlers of the host-core commands: hosts, doctor, devices, lease, job, run, pipeline, collect, compare, baseline, gc, retain.

Every command that works on a host takes `--host <id>` to run there instead (over the transport, with the remote project's
the recorded remote command); `run` and `pipeline` choose the host themselves through `HostResolver` and record the choice in the result.
"""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Sequence

from . import artifacts, buildcache, compare as comparing, config, constants, core, diskspace, doctor, hostsync, jobs, lease, outputs, project, providers, remote, scratch, submit, supervise, wheel, workroot
from . import __version__
from .adapter import DeviceInfo
from .cli import Command, register
from .pipeline import validate_prelaunch
from .resolver import HostResolver, Request, SELECTED_LOCAL
from .schema import SchemaError, UnreadableInput, Validator, validate_document

REMOTE_TIMEOUT_SEC = 90


def _out(document: Any) -> None:
    print(json.dumps(document, indent=2, ensure_ascii=False))


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _blocked(command: str, message: str) -> int:
    _err(f"valtrion {command}: BLOCKED: {message}")
    return constants.EXIT_BLOCKED


def _add_host_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", help="run on this configured host (see `valtrion hosts`) instead of this machine")
    parser.add_argument("--remote-project", help="working directory on the SSH host (or set GDV_REMOTE_PROJECT); the command itself is the one recorded by `valtrion hosts sync`")


def _entry(arguments: argparse.Namespace) -> config.HostEntry | None:
    """The remote host named by --host, or None for this machine."""
    host_id = getattr(arguments, "host", None)
    if not host_id:
        return None
    entry = config.find_host(host_id)
    return None if entry.kind == "local" else entry


def _remote(entry: config.HostEntry, arguments: argparse.Namespace, retry: bool = True, lifeline: bool = False) -> remote.Remote:
    """The valtrion entry on `entry`. Commands that start something (not idempotent) pass `retry=False`: a retried start could run twice.
    A foreground command that must stop when the connection ends passes `lifeline=True` (see `SshTransport.lifeline`)."""
    project = getattr(arguments, "remote_project", None) or os.environ.get("GDV_REMOTE_PROJECT")
    return remote.remote_for(entry, project, remote.make_transport(entry, retry=retry, lifeline=lifeline))


def _strip_host_options(raw: Sequence[str]) -> list[str]:
    """Drop --host and --remote-project from the command line to forward; everything after `--` is the user's command, kept as is."""
    result, skip = [], False
    for position, item in enumerate(raw):
        if item == "--":
            return result + list(raw[position:])
        if skip:
            skip = False
        elif item in ("--host", "--remote-project"):
            skip = True
        elif not item.startswith(("--host=", "--remote-project=")):
            result.append(item)
    return result


def _forward(arguments: argparse.Namespace, entry: config.HostEntry, extra: Sequence[str] = (), timeout: int = REMOTE_TIMEOUT_SEC,
             retry: bool = True, lost_hint: str = "", lifeline: bool = False) -> int:
    """Run the same command on the SSH host and relay its output and exit code. Only idempotent commands use this with `retry`.
    `lost_hint` is added to the message when the answer is lost (a command that may still run on the host says so)."""
    try:
        target = _remote(entry, arguments, retry, **({"lifeline": True} if lifeline else {}))
    except ValueError as error:
        return _blocked(arguments.command, str(error))
    line = _strip_host_options(arguments.raw_argv)
    place = line.index("--") if "--" in line else len(line)  # `extra` options go before the user's own command after `--`
    result = target.call([*line[:place], *extra, *line[place:]], timeout)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.timed_out:
        return _blocked(arguments.command, f"host {entry.id} did not answer within {timeout} s{lost_hint}")
    if result.returncode == remote.SSH_FAILURE:
        return _blocked(arguments.command, f"host {entry.id} is unreachable ({_failure_evidence(target, result)}){lost_hint}")
    if remote.is_tool_missing(result):
        _err(f"valtrion {arguments.command}: host {entry.id} is reachable, but the valtrion command is missing there; run `valtrion hosts sync`")
    return result.returncode if result.returncode is not None else constants.EXIT_BLOCKED


def _failure_evidence(target: remote.Remote, result: remote.CommandResult) -> str:
    """The last error and the number of tries behind an `unreachable` verdict."""
    return remote.final_verdict(result, target.attempts).detail


def _unity_project(arguments: argparse.Namespace) -> Path | None:
    """The Unity project: --project-dir, else GDV_UNITY_PROJECT, else the project named by the nearest valtrion.json."""
    try:
        found = project.resolve_unity_project(arguments.project_dir)
    except project.ProjectError as error:
        raise SystemExit(_blocked(arguments.command, str(error)))
    if found is not None and getattr(arguments, "local", False) and getattr(arguments, "transport", "") == "ssh" and not found.is_dir():
        # A run sent here by a caller: the project must exist on this host, whatever path the caller thinks it has.
        raise SystemExit(_blocked(arguments.command, f"the Unity project folder {found} does not exist on host {core.local_host_id()}"))
    return found


def _lease_wait(value: float | None, fallback: float = 0.0) -> float:
    """The wait for leases: the caller's `--lease-wait`, else `leaseWaitSec` of host.json, else `fallback`."""
    if value is not None:
        return value
    configured = config.default_lease_wait()
    return fallback if configured is None else configured


def _owner() -> str:
    return os.environ.get("GDV_OWNER") or f"{getpass.getuser()}@{core.local_host_id()}"


# -- hosts ------------------------------------------------------------------------------------------------------------------


def _configure_hosts(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="action")
    sub.add_parser("list", help="list configured hosts (default)")
    add = sub.add_parser("add", help="add or replace an SSH host in hosts.json")
    add.add_argument("--id", required=True)
    add.add_argument("--ssh", required=True, help="ssh target, user@host")
    add.add_argument("--os", required=True, choices=("windows", "macos", "linux"))
    add.add_argument("--artifacts-root", required=True)
    add.add_argument("--projects-root")
    add.add_argument("--connect-timeout", type=int)
    add.add_argument("--python", help="absolute path of a Python 3.10+ on the host (hosts sync finds one when this is not given)")
    sync = sub.add_parser("sync", help="install this core version on every SSH host and record the remote valtrion command")
    sync.add_argument("--only", action="append", default=[], metavar="HOST", help="sync only this host (repeatable)")
    sync.add_argument("--prune", action="store_true", help="on a host with pipx also remove old core/<version> folders (older than this "
                      "version, unused); without pipx they are always cleaned up")
    sync.add_argument("--ensure-path", action="store_true", help="also add the folder of the recorded command to the PATH of "
                      "non-interactive shells on the host (macOS: a line in ~/.zshenv); nothing like this is done without the flag")
    parser.add_argument("--json", action="store_true")


def _hosts(arguments: argparse.Namespace) -> int:
    try:
        if arguments.action == "add":
            path = config.add_ssh_host(arguments.id, arguments.ssh, arguments.os, arguments.artifacts_root,
                                       projects_root=arguments.projects_root, connect_timeout_sec=arguments.connect_timeout, python=arguments.python)
            print(f"added {arguments.id} to {path}")
            return constants.EXIT_PASS
        entries = config.load_hosts()
        if arguments.action == "sync":
            return _hosts_sync(arguments, entries)
    except config.ConfigError as error:
        return _blocked("hosts", str(error))
    rows = [{"id": e.id, "transport": e.kind, "os": e.os, "target": e.ssh_target, "artifactsRoot": e.artifacts_root_text or str(e.artifacts_root),
             "projectsRoot": e.projects_root, "auto": e.auto, "valtrionCommand": e.valtrion_command, "python": e.python} for e in entries]
    if arguments.json:
        _out(rows)
    else:
        for row in rows:
            where = row["target"] or "this machine"
            print(f"{row['id']:<20} {row['transport']:<6} {row['os']:<8} {where}{'  (added automatically)' if row['auto'] else ''}")
    return constants.EXIT_PASS


def _hosts_sync(arguments: argparse.Namespace, entries: Sequence[config.HostEntry]) -> int:
    remote_entries = [e for e in entries if e.kind == "ssh" and (not arguments.only or e.id in arguments.only)]
    unknown = sorted(set(arguments.only) - {e.id for e in remote_entries})
    if unknown:
        return _blocked("hosts", f"no SSH host named {', '.join(unknown)}")
    if not remote_entries:
        print("no SSH hosts are configured; nothing to sync")
        return constants.EXIT_PASS
    import tempfile
    with tempfile.TemporaryDirectory(prefix="valtrion-wheel-") as folder:
        built = wheel.build_wheel(Path(folder))
        rows = []
        for entry in remote_entries:
            transport = remote.make_transport(entry)
            row = hostsync.sync_host(entry, transport, built, config.set_host_command, prune_pipx=arguments.prune)
            if arguments.ensure_path and row.command and row.status in (hostsync.OK, hostsync.UPDATED):
                note = hostsync.ensure_path(entry, transport, row.command)
                row = dataclasses.replace(row, reason="; ".join(part for part in (row.reason, note) if part))
            rows.append(row)
    if arguments.json:
        _out([row.document() for row in rows])
    else:
        print(hostsync.render(rows))
    return constants.EXIT_PASS if hostsync.all_synced(rows) else constants.EXIT_BLOCKED


# -- doctor and devices -----------------------------------------------------------------------------------------------------


def discover_all(timeout_sec: float = 90.0) -> tuple[list[DeviceInfo], dict[str, str], list[str]]:
    """Live discovery through every registered adapter. Returns (devices, errors by platform, platforms discovered)."""
    devices: list[DeviceInfo] = []
    errors: dict[str, str] = {}
    done: list[str] = []
    for platform in providers.platforms():
        adapter = providers.get(platform)
        if adapter is None:
            continue
        try:
            finished, found = supervise.call_bounded(lambda a=adapter: list(a.discover()), timeout_sec)
        except Exception as error:  # noqa: BLE001 - adapter code; one broken adapter must not hide the others
            errors[platform] = f"{type(error).__name__}: {error}"
            continue
        if not finished:
            errors[platform] = f"discovery did not finish within {timeout_sec:g} s"
            continue
        devices.extend(found or [])
        done.append(platform)
    return devices, errors, done


def apply_adapter_capabilities(report: dict, timeout_sec: float = 60.0) -> None:
    """Adds what each installed adapter says it can do. A platform whose adapter cannot build here is no longer a build platform, and
    the reason is listed under `blocked`, so the host resolver never sends a build to a host that would refuse it."""
    adapters: dict[str, dict] = {}
    for platform in providers.platforms():
        adapter = providers.get(platform)
        if adapter is None:
            continue
        try:
            finished, found = supervise.call_bounded(adapter.probe, timeout_sec)
        except Exception as error:  # noqa: BLE001 - adapter code; one broken adapter must not hide the others
            adapters[platform] = {"available": [], "blocked": {"probe": f"{type(error).__name__}: {error}"}}
            continue
        if not finished or found is None:
            adapters[platform] = {"available": [], "blocked": {"probe": f"probe did not finish within {timeout_sec:g} s"}}
            continue
        adapters[platform] = {"available": list(found.available), "blocked": dict(found.blocked)}
        if "build" in found.blocked and platform in report["capabilities"]["buildPlatforms"]:
            report["capabilities"]["buildPlatforms"] = [p for p in report["capabilities"]["buildPlatforms"] if p != platform]
            report["blocked"][f"{platform}.build"] = found.blocked["build"]
    if adapters:
        report["adapters"] = adapters


def local_report(with_devices: bool, home: Path | None = None) -> dict:
    settings = config.load_host_settings(home)
    report = doctor.probe_local(artifacts_root=settings.artifacts_root, work_root=settings.work_root, min_free_gib=settings.min_free_gib,
                                  projects=settings.projects, checkout_sizes=True)
    accelerator = doctor.accelerator_report(settings.unity_accelerator)
    if accelerator is not None:
        report["unityAccelerator"] = accelerator
    registry = config.load_registry(home)
    if with_devices:
        devices, errors, done = discover_all()
        registry = config.merge_discovered(registry, devices, done, core.local_host_id())
        config.save_registry(registry, home)
        if errors:
            report["discoveryErrors"] = errors
    report["devices"] = registry.get("devices", [])
    apply_adapter_capabilities(report)
    if providers.load_errors():
        report["adapterErrors"] = providers.load_errors()
    return report


def _configure_doctor(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--devices", action="store_true", help="also run live device discovery and refresh the registry")
    parser.add_argument("--project", nargs="?", const="", metavar="DIR", help="also check that the Unity package of the project (default: "
                        "GDV_UNITY_PROJECT or the project named by valtrion.json) is supported by this core")
    _add_host_options(parser)


def _project_check(arguments: argparse.Namespace) -> dict | None:
    if arguments.project is None:
        return None
    try:
        folder = project.resolve_unity_project(Path(arguments.project) if arguments.project else None)
    except project.ProjectError as error:
        return {"status": "BLOCKED", "reason": str(error), "action": "fix valtrion.json or pass the project folder"}
    if folder is None:
        return {"status": "skipped", "reason": "no Unity project given (no --project DIR, GDV_UNITY_PROJECT or valtrion.json)"}
    return {**project.check_project(folder), "project": str(folder)}


def _remote_doctor(arguments: argparse.Namespace, entry: config.HostEntry) -> tuple[int, dict | None]:
    """Run `doctor --json` on the SSH host and compare its core version with this one. Returns (exit code, report)."""
    try:
        target = _remote(entry, arguments)
    except ValueError as error:
        return _blocked("doctor", str(error)), None
    extra = ["--devices"] if arguments.devices else []
    result, report = target.call_json(["doctor", "--json", *extra], 180)
    if result.timed_out or result.returncode == remote.SSH_FAILURE:
        return _blocked("doctor", f"host {entry.id} is unreachable ({_failure_evidence(target, result)})"), None
    if report is None and remote.is_tool_missing(result):
        return _blocked("doctor", f"host {entry.id} is reachable, but the valtrion command is missing there "
                                  f"({_failure_evidence(target, result)}); run `valtrion hosts sync`"), None
    if report is None:
        _err(result.stderr.strip())
        return _blocked("doctor", f"host {entry.id} did not return a doctor report (exit {result.returncode})"), None
    found = report.get("core", {}).get("version")
    if found != __version__:
        return _blocked("doctor", f"host {entry.id} runs valtrion {found or 'unknown'}, this machine runs {__version__}; "
                                  "run `valtrion hosts sync`"), report
    check = hostsync.path_check(entry, target.transport)
    if check is not None:
        report["valtrionOnPath"] = check
        if check["status"] != "ok":
            _err(f"valtrion doctor: warning: host {entry.id}: {check['reason']}; {check['action']}")
    return constants.EXIT_PASS, report


def _doctor(arguments: argparse.Namespace) -> int:
    entry = _entry(arguments)
    code = constants.EXIT_PASS
    try:
        if entry is not None:
            code, report = _remote_doctor(arguments, entry)
            if report is None:
                return code
        else:
            report = local_report(arguments.devices)
    except config.ConfigError as error:
        return _blocked("doctor", str(error))
    check = _project_check(arguments)
    if check is not None:
        report["project"] = check
        if check["status"] == "BLOCKED":
            code = constants.EXIT_BLOCKED
            _err(f"valtrion doctor: BLOCKED: {check['reason']}" + (f"; {check['action']}" if check.get("action") else ""))
    if arguments.json:
        _out(report)
    else:
        print(doctor.render(report))
        for item in report["devices"]:
            print(f"  device {item['id']} ({item['class']}, {item.get('evidence', 'physical')})" + ("" if config.is_online(item) else " OFFLINE"))
        if check is not None:
            print(f"  project: {check['status']} - {check['reason']}")
    return code


def _configure_devices(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--refresh", action="store_true", help="run discovery through the adapters and update the registry; a registered "
                        "device that is not found stays registered as offline, with its class, capabilities and alias")
    parser.add_argument("--forget", metavar="DEVICE", help="remove a device (id or alias) and everything set for it from the registry")
    parser.add_argument("--json", action="store_true")
    _add_host_options(parser)


def _devices(arguments: argparse.Namespace) -> int:
    entry = _entry(arguments)
    if entry is not None:
        return _forward(arguments, entry, timeout=180)
    if arguments.forget and arguments.refresh:
        return _blocked("devices", "--forget and --refresh are separate actions; run them one after the other")
    try:
        registry = config.load_registry()
        errors: dict[str, str] = {}
        if arguments.forget:
            registry, gone = config.forget_device(registry, arguments.forget)
            if gone is None:
                _err(f"valtrion devices: no registered device is named {arguments.forget}")
                return constants.EXIT_FAIL
            config.save_registry(registry)
            print(f"forgot {gone['id']} ({gone['class']}, capabilities {', '.join(gone.get('capabilities', [])) or '-'})")
            return constants.EXIT_PASS
        if arguments.refresh:
            devices, errors, done = discover_all()
            registry = config.merge_discovered(registry, devices, done, core.local_host_id())
            config.save_registry(registry)
    except config.ConfigError as error:
        return _blocked("devices", str(error))
    if arguments.json:
        _out({"devices": registry["devices"], "errors": errors})
    else:
        for item in registry["devices"]:
            state = "" if config.is_online(item) else f"  OFFLINE (last seen {item.get('lastSeen', 'unknown')})"
            print(f"{item['id']:<32} {item['platform']:<8} {item['class']:<16} {item.get('evidence', 'physical'):<9} {item.get('os', '')}{state}")
        for platform, message in errors.items():
            _err(f"discovery {platform}: {message}")
        if not registry["devices"]:
            print("no devices" + ("" if arguments.refresh else " in the registry; run `valtrion devices --refresh`"))
    return constants.EXIT_BLOCKED if errors and not registry["devices"] else constants.EXIT_PASS


# -- leases -----------------------------------------------------------------------------------------------------------------


def _configure_lease(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("acquire", "release", "renew"):
        child = sub.add_parser(name)
        child.add_argument("resource")
        child.add_argument("--session", default=os.environ.get("GDV_SESSION"))
        child.add_argument("--holder-pid", type=int, default=None,
                           help="process that holds the lease (default: the parent of this command); it must stay alive and renew it")
        child.add_argument("--owner", default=None)
        _add_host_options(child)
        if name == "acquire":
            child.add_argument("--slots", type=int, help="treat RESOURCE as a counted resource with this many slots")
            child.add_argument("--ttl", type=int, default=120)
            child.add_argument("--wait", type=float, default=0, help="seconds to wait for a busy resource before BLOCKED")
            child.add_argument("--purpose", default="")
            child.add_argument("--worktree", default="")
    for name in ("status", "list"):
        child = sub.add_parser(name)
        if name == "status":
            child.add_argument("resource")
        _add_host_options(child)
    name_parser = sub.add_parser("name", help="print the lease name of a resource (read-only)")
    name_parser.add_argument("kind", choices=("unity-project",), help="unity-project: the lease that guards one Unity project folder")
    name_parser.add_argument("path", type=Path, help="the project folder")
    _add_host_options(name_parser)


def _lease(arguments: argparse.Namespace) -> int:
    entry = _entry(arguments)
    if entry is not None:
        wait = getattr(arguments, "wait", 0) or 0
        return _forward(arguments, entry, timeout=int(wait) + REMOTE_TIMEOUT_SEC)
    if arguments.action == "name":
        _out({"resource": lease.unity_project_resource(arguments.path)})
        return constants.EXIT_PASS
    manager = lease.LeaseManager()
    action = arguments.action
    resource = getattr(arguments, "resource", None)
    if resource is not None:
        try:
            lease.lease_file_name(resource)
        except ValueError as error:
            return _blocked("lease", str(error))
    if action == "list":
        _out([{"resource": s.resource, "state": s.state, "lease": s.record.to_document() if s.record else None} for s in manager.list_all()])
        return constants.EXIT_PASS
    if action == "status":
        status = manager.status(arguments.resource)
        _out({"resource": status.resource, "state": status.state, "lease": status.record.to_document() if status.record else None})
        return constants.EXIT_PASS
    session = arguments.session or f"cli-{os.getppid()}"
    pid = arguments.holder_pid if arguments.holder_pid is not None else os.getppid()
    owner = arguments.owner or _owner()
    if action == "acquire":
        def waiting(reason: str, waited_sec: float) -> None:
            _err(lease.waiting_message(reason, waited_sec))

        try:
            slots = arguments.slots
            if slots is None and arguments.resource == lease.UNITY_EDITOR_RESOURCE:
                slots = lease.unity_editor_capacity()  # the host's total, the same one every other claimant counts
            if slots:
                outcome = manager.acquire_slot(arguments.resource, slots, owner, session, arguments.ttl, arguments.purpose, pid,
                                               arguments.worktree, arguments.wait, on_wait=waiting)
            else:
                outcome = manager.acquire(arguments.resource, owner, session, arguments.ttl, arguments.purpose, pid, arguments.worktree,
                                          arguments.wait, on_wait=waiting)
        except ValueError as error:
            return _blocked("lease", str(error))
        if not outcome.acquired:
            return _blocked("lease", outcome.reason or "busy")
        _out({"acquired": True, "lease": outcome.lease.to_document(),
              "reclaimedStale": outcome.reclaimed.to_document() if outcome.reclaimed else None})
        return constants.EXIT_PASS
    if action == "renew":
        ok = manager.renew(arguments.resource, session, pid, owner if arguments.owner else None)
        _out({"renewed": ok})
        return constants.EXIT_PASS if ok else constants.EXIT_BLOCKED
    ok = manager.release(arguments.resource, session, pid)
    _out({"released": ok})
    return constants.EXIT_PASS if ok else constants.EXIT_BLOCKED


# -- jobs -------------------------------------------------------------------------------------------------------------------


def _watch_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from", dest="from_offset", type=int, default=0, help="events.jsonl byte offset to resume from")
    parser.add_argument("--stall-sec", type=float, help="stall window (default: the job's own)")
    parser.add_argument("--poll-sec", type=float, default=2.0)
    parser.add_argument("--unreachable-sec", type=float, default=1800.0)
    parser.add_argument("--no-collect", action="store_true", help="do not pull a remote run's artifacts when it ends")


def _configure_job(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="action", required=True)
    start = sub.add_parser("start", help="start a command as a detached, supervised job; prints the job id and returns")
    start.add_argument("--kind", default="run", choices=jobs.JOB_KINDS)
    start.add_argument("--project", default="adhoc")
    start.add_argument("--sha", help="full commit sha the job works on")
    start.add_argument("--label", default="job")
    start.add_argument("--stall-sec", type=float, default=supervise.DEFAULT_STALL_SEC)
    start.add_argument("--timeout-sec", type=float, required=True)
    start.add_argument("--progress-file", action="append", default=[], help="file or folder whose growth is progress (repeatable)")
    start.add_argument("--heartbeat", help="a gdv.heartbeat/1 file whose seq must advance (log growth then no longer counts)")
    start.add_argument("--cpu-progress", action="store_true", help="CPU time of the process tree counts as progress")
    start.add_argument("--contract-exit-codes", action="store_true", help="read exit 2 as BLOCKED and 3 as TIMEOUT")
    start.add_argument("--lease", action="append", default=[], help="resource[=slots] to hold for the job (repeatable)")
    start.add_argument("--lease-wait", type=float, default=None, help="seconds to wait for a leased resource; default: leaseWaitSec of host.json, 0")
    start.add_argument("--lease-ttl", type=int, default=jobs.DEFAULT_LEASE_TTL_SEC)
    start.add_argument("--cwd")
    start.add_argument("--cleanup", action="append", default=[], metavar="PATH",
                       help="output path, relative to --cwd, to remove when the job ends (repeatable; never a tracked file or a path outside --cwd)")
    start.add_argument("--domain", choices=("auto", "user", "gui"), default="auto",
                       help="macOS launchd domain: auto tries user, then gui; gui is required for signing steps")
    start.add_argument("--foreground", action="store_true", help="run in this process, not in a launchd domain (debugging only; not for signing: over SSH it cannot reach the login keychain; cannot be combined with --domain user|gui)")
    start.add_argument("--local", action="store_true", help=argparse.SUPPRESS)
    start.add_argument("--selected-by", default=SELECTED_LOCAL, help=argparse.SUPPRESS)
    start.add_argument("--transport", default="local", help=argparse.SUPPRESS)
    start.add_argument("--watch", action="store_true", help="after starting, follow the job to its end")
    _add_host_options(start)
    start.add_argument("command_line", nargs=argparse.REMAINDER, help="-- command and arguments")
    for name in ("status", "tail", "watch", "cancel"):
        child = sub.add_parser(name)
        child.add_argument("job_id")
        _add_host_options(child)
        if name in ("tail", "watch"):
            _watch_options(child)
    run = sub.add_parser("_run", help=argparse.SUPPRESS)
    run.add_argument("directory", type=Path)


def _job_directory(job_id: str) -> Path:
    return jobs.find_job_directory(job_id)


def _split_lease(text: str) -> dict:
    resource, _, slots = text.partition("=")
    return {"resource": resource, "slots": int(slots)} if slots else {"resource": resource}


def _job(arguments: argparse.Namespace) -> int:
    action = arguments.action
    if action == "_run":
        return jobs.run_job(arguments.directory)
    entry = _entry(arguments)
    try:
        if action == "start":
            return _job_start(arguments, entry)
        if action == "watch":
            return _job_watch(arguments, entry)
        if entry is not None:
            return _forward(arguments, entry)
        directory = _job_directory(arguments.job_id)
        if action == "status":
            _out(jobs.JobStore(directory).read_job())
        elif action == "tail":
            _out(jobs.tail_info(directory, arguments.from_offset, config.load_host_settings().artifacts_root))
        else:
            _out(jobs.cancel_job(directory))
        return constants.EXIT_PASS
    except (jobs.JobError, config.ConfigError, ValueError) as error:
        return _blocked("job", str(error))


def _job_start(arguments: argparse.Namespace, entry: config.HostEntry | None) -> int:
    # Only the separator in front of the command is dropped; a `--` inside the user's own command stays.
    command_line = arguments.command_line[1:] if arguments.command_line[:1] == ["--"] else arguments.command_line
    if not command_line or "--" not in arguments.raw_argv:
        return _blocked("job", "give the command after `--`")
    if entry is not None:
        extra = ["--local", "--transport", "ssh", "--selected-by", "pinned"]
        raw = _strip_host_options(arguments.raw_argv)
        split = raw.index("--")
        raw = [item for item in raw[:split] if item != "--watch"] + extra + raw[split:]
        try:
            result = _remote(entry, arguments, retry=False).call(raw, REMOTE_TIMEOUT_SEC)
        except ValueError as error:
            return _blocked("job", str(error))
        if not result.ok:
            return _blocked("job", f"host {entry.id} could not start the job: {(result.stderr or result.stdout).strip()[:300]}")
        sys.stdout.write(result.stdout)
        if arguments.watch:
            started = remote.extract_json_object(result.stdout) or {}
            if "jobId" not in started:
                return _blocked("job", f"host {entry.id} started the job but printed no job id to watch")
            arguments.job_id = started["jobId"]
            arguments.from_offset, arguments.stall_sec, arguments.poll_sec, arguments.unreachable_sec, arguments.no_collect = 0, None, 2.0, 1800.0, False
            return _job_watch(arguments, entry)
        return constants.EXIT_PASS
    settings = config.load_host_settings()
    host = {"id": core.local_host_id(), "transport": arguments.transport, "selectedBy": arguments.selected_by}
    cwd, cleanup_paths = arguments.cwd, []
    if arguments.cleanup:
        # Declared outputs are checked now, on the host that runs the job: a refused path is an error, the job does not start.
        if not arguments.cwd:
            return _blocked("job", "--cleanup needs --cwd: the working directory its paths are relative to")
        base = Path(arguments.cwd).resolve()
        if not base.is_dir():
            return _blocked("job", f"--cwd {arguments.cwd} is not a folder on this host")
        try:
            cleanup_paths = outputs.declare(base, arguments.cleanup)
        except outputs.OutputError as error:
            return _blocked("job", str(error))
        cwd = str(base)  # the command runs where the paths are judged, whatever the runner's own working directory is
    request = jobs.JobRequest(
        kind=arguments.kind, project=arguments.project, sha=arguments.sha, label=arguments.label,
        payload={"type": "command", "argv": command_line, "cwd": cwd}, host=host, stall_sec=arguments.stall_sec,
        timeout_sec=arguments.timeout_sec, progress_files=tuple(arguments.progress_file), heartbeat=arguments.heartbeat,
        cpu_counts=arguments.cpu_progress, contract_exit_codes=arguments.contract_exit_codes,
        leases=tuple(_split_lease(item) for item in arguments.lease), lease_wait_sec=_lease_wait(arguments.lease_wait), lease_ttl_sec=arguments.lease_ttl,
        owner=_owner(), purpose=f"valtrion job {arguments.label}", cleanup_paths=tuple(cleanup_paths),
        cleanup_base=cwd if cleanup_paths else None)
    info = jobs.start_job(request, settings.artifacts_root, domain=arguments.domain, foreground=arguments.foreground,
                          work_roots=settings.roots)
    _out(info)
    if arguments.watch and not arguments.foreground:
        arguments.job_id = info["jobId"]
        arguments.from_offset, arguments.stall_sec, arguments.poll_sec, arguments.unreachable_sec, arguments.no_collect = 0, None, 2.0, 1800.0, False
        return _job_watch(arguments, None)
    return info.get("exitCode", constants.EXIT_PASS)


def _job_watch(arguments: argparse.Namespace, entry: config.HostEntry | None) -> int:
    root = config.load_host_settings().artifacts_root
    if entry is None:
        directory = _job_directory(arguments.job_id)

        def fetch(offset: int) -> dict | None:
            return jobs.tail_info(directory, offset, root)
    else:
        target = _remote(entry, arguments)

        def fetch(offset: int) -> dict | None:
            result = target.call(["job", "tail", arguments.job_id, "--from", str(offset)], 60)
            if result.timed_out or result.returncode in (remote.SSH_FAILURE, None):
                return None
            if not result.ok:
                raise jobs.JobError((result.stderr or result.stdout).strip()[:300] or "the host rejected the request")
            return remote.extract_json_object(result.stdout)
    outcome = jobs.watch(fetch, lambda line: print(line, flush=True), arguments.from_offset, arguments.stall_sec, arguments.poll_sec,
                         arguments.unreachable_sec)
    note = ""
    if entry is not None and outcome.info and not arguments.no_collect and outcome.state in ("finished", "cancelled"):
        rel = outcome.info.get("relDir")
        if rel:
            pulled = remote.pull_run(remote.make_transport(entry), outcome.info["dir"], root / rel)
            note = f" collected={'yes' if pulled.ok else 'FAILED: ' + (pulled.stderr or '').strip()[:120]}"
    reason = f" reason={outcome.reason}" if outcome.reason else ""
    print(f"GDV-RESULT {outcome.status} job={arguments.job_id} offset={outcome.offset}{note}{reason}", flush=True)
    return outcome.exit_code


# -- run and pipeline -------------------------------------------------------------------------------------------------------


def _load_document(path: Path, command: str) -> tuple[dict | None, int]:
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        return None, _blocked(command, f"cannot read {path}: {error}")
    _, errors = validate_document(document)
    if errors:
        for message in errors:
            _err(f"{path}: {message}")
        return None, constants.EXIT_FAIL
    return document, 0


#: Platforms whose build signs with the login keychain: on macOS their jobs start in the `gui` launchd domain by default.
SIGNING_PLATFORMS = ("ios", "macos")


def _request_for(pipeline: dict, base: Path, project_dir: Path | None) -> Request:
    build = any(s["stage"] == "build" for s in pipeline["stages"])
    device = any(s["stage"] in ("install", "run") for s in pipeline["stages"])
    platform = pipeline["device"].get("platform")
    capabilities: list[str] = []
    for stage in pipeline["stages"]:
        if stage["stage"] == "run" and stage.get("plan"):
            try:
                plan = json.loads((base / stage["plan"]).read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            platform = platform or (plan.get("requires", {}).get("platforms") or [None])[0]
            capabilities.extend(plan.get("requires", {}).get("capabilities", []))
    from . import pipeline as pipelines
    version = pipelines.unity_version(project_dir) if build else None
    if version == "unknown":
        # The project is not on this machine (a Mac asking a Windows host to build): the host that runs the job reads the
        # project's Unity version itself and the build stage is BLOCKED there when that editor is missing.
        version = None
    return Request(platform or "editor", build, version, device,
                   pipeline["device"].get("class"), tuple(dict.fromkeys(capabilities)))


def resolve_host(request: Request, pinned: str | None, remote_project: str | None = None):
    entries = config.load_hosts()
    local = next(e for e in entries if e.kind == "local")
    others = [e for e in entries if e.kind != "local"]
    by_id = {e.id: e for e in entries}

    def remote_factory(entry: config.HostEntry):
        try:
            return remote.remote_for(entry, remote_project or os.environ.get("GDV_REMOTE_PROJECT"))
        except remote.RemotePathError:
            raise  # the probe reports it as the reason, not as an unreachable host
        except ValueError:
            return None

    probe = remote.make_probe(lambda: local_report(True), remote_factory, by_id)
    resolver = HostResolver(local.info(), [e.info() for e in others], probe)
    from dataclasses import replace
    return resolver.resolve(replace(request, pinned_host=pinned)), by_id


def _managed_project(wanted: str, candidate: dict, payload: dict) -> Path:
    """The Unity project folder of the managed checkout `wanted` (an id, or `auto`) of `candidate.repo` on this host; the job's payload
    says which checkout it is and how to create it. `ValueError` says why a job cannot use one."""
    sha = candidate.get("sha")
    if not sha or not re.fullmatch(r"[0-9a-f]{40}", sha) or set(sha) == {"0"}:
        raise ValueError("--checkout needs the commit to build: give --sha (the full 40-character commit)")
    settings = config.load_host_settings()
    managed = settings.project(candidate["repo"])
    if managed is None:
        raise ValueError(f"host.json declares no project {candidate['repo']!r} (projects.<name>.checkouts), so --checkout cannot be used")
    chosen = managed.pick(None if wanted == "auto" else wanted)
    if chosen is None:
        raise ValueError(f"project {managed.name} has no checkout {wanted!r}; it has " + ", ".join(item.id for item in managed.checkouts))
    payload.update({"projectDir": str(managed.project_dir(chosen)), "checkoutId": chosen.id, "checkoutRoot": str(chosen.path),
                    "checkoutRemote": managed.remote, "unityPath": managed.unity_path, "shaPinned": True})
    return managed.project_dir(chosen)


def _preflight(pipeline: dict, base: Path, payload: dict, project_dir: Path | None) -> str | None:
    """What would end the job `BLOCKED` before any stage runs, found at submit: the run stages' plans and pre-launch files, a build stage
    without a project, and (for a managed checkout) a commit that is neither in the checkout nor on its
    remote. None when the job can go on to wait for its leases."""
    from . import checkout as checkouts
    problem = validate_prelaunch(pipeline["stages"], base)
    if problem:
        return problem
    for stage in pipeline["stages"]:
        if stage["stage"] == "run" and stage.get("plan"):
            plan = Path(stage["plan"])
            plan = plan if plan.is_absolute() else (base / plan)
            try:
                _, errors = validate_document(json.loads(plan.read_text(encoding="utf-8-sig")))
            except (OSError, ValueError) as error:
                return f"cannot read the plan {stage['plan']}: {error}"
            if errors:
                return f"plan {stage['plan']} is invalid: " + "; ".join(errors[:3])
    if any(stage["stage"] == "build" for stage in pipeline["stages"]) and project_dir is None:
        return "a build stage needs --project-dir (or --checkout for a managed project)"
    if payload.get("checkoutRoot"):
        return checkouts.preflight(Path(payload["checkoutRoot"]), pipeline["candidate"]["sha"], payload.get("checkoutRemote"),
                                   comparing.run_git, lambda folder, arguments: comparing.run_git(folder, arguments, timeout=600))
    return None


def _start_pipeline(arguments: argparse.Namespace, pipeline: dict, payload: dict, base: Path, project_dir: Path | None, label: str) -> int:
    if arguments.local:
        entry, selected = config.local_host(), arguments.selected_by
        transport = arguments.transport
    else:
        resolution, by_id = resolve_host(_request_for(pipeline, base, project_dir), arguments.host,
                                           getattr(arguments, "remote_project", None))
        if resolution.host is None:
            for reason in resolution.reasons:
                _err(f"  {reason}")
            return _blocked(arguments.command, "no host can run this request")
        entry, selected, transport = by_id[resolution.host.id], resolution.selected_by, resolution.transport
        if entry.kind == "ssh":
            extra = ["--local", "--transport", "ssh", "--selected-by", selected or "capability"]
            return _start_remote_pipeline(arguments, entry, extra, pipeline, project_dir)
    settings_root = entry.artifacts_root
    host = {"id": entry.id, "transport": transport or "local", "selectedBy": selected or SELECTED_LOCAL}
    candidate = pipeline["candidate"]
    if getattr(arguments, "checkout", None):
        project_dir = _managed_project(arguments.checkout, candidate, payload)
    sha = candidate.get("sha")
    if (not sha or set(sha) == {"0"}) and project_dir is not None:
        # The run folder is named after the commit, so read it from the checkout that is built (the result records the same one).
        code, head = comparing.run_git(project_dir, ["rev-parse", "HEAD"])
        if code == 0 and re.fullmatch(r"[0-9a-f]{40}", head):
            sha = head
        else:
            _err(f"valtrion: the commit of {project_dir} could not be read ({' '.join(head.split())[:200] or 'no message'}); "
                 "the run folder is `unversioned` and the result will carry no commit")
    if sha and set(sha) == {"0"}:
        sha = None  # no commit known: the run folder is `unversioned`, not `00000000`
    problem = _preflight(pipeline, base, payload, project_dir)
    if problem:
        return _blocked(arguments.command, problem)  # before the job exists: nothing was leased and nothing waits in a queue
    try:
        # Run stages' `cleanup`: outputs relative to the project folder, removed when the job ends; a refused path stops the job here.
        declared = outputs.stage_paths(pipeline["stages"])
        if declared and project_dir is None:
            return _blocked(arguments.command, "a run stage's `cleanup` needs the project folder its paths are relative to "
                                               "(--project-dir, --remote-project or --checkout)")
        cleanup_paths = outputs.declare(project_dir, declared) if declared else []
    except outputs.OutputError as error:
        return _blocked(arguments.command, str(error))
    request = jobs.JobRequest(
        kind="pipeline", project=candidate["repo"], sha=sha, label=label, payload=payload, host=host,
        stall_sec=arguments.stall_sec, timeout_sec=arguments.timeout_sec, owner=_owner(), purpose=f"valtrion {arguments.command} {label}",
        lease_wait_sec=_lease_wait(arguments.lease_wait), protected_paths=tuple(arguments.protect), disk_wait_sec=getattr(arguments, "disk_wait", None),
        cleanup_paths=tuple(cleanup_paths), cleanup_base=str(project_dir.resolve()) if cleanup_paths and project_dir is not None else None)
    domain = arguments.domain
    if domain == "auto" and _request_for(pipeline, base, project_dir).platform in SIGNING_PLATFORMS:
        domain = "gui"  # signing needs the login keychain, which only the GUI session has
    info = jobs.start_job(request, settings_root, domain=domain, foreground=arguments.foreground,
                          work_roots=config.load_host_settings().roots)
    _out(info)
    if arguments.watch and not arguments.foreground:
        arguments.job_id = info["jobId"]
        arguments.from_offset, arguments.stall_sec, arguments.poll_sec, arguments.unreachable_sec, arguments.no_collect = 0, None, 2.0, 1800.0, False
        return _job_watch(arguments, None)
    return info.get("exitCode", constants.EXIT_PASS)


def _remote_command_line(arguments: argparse.Namespace, entry: config.HostEntry, pipeline: dict, project_dir: Path | None) -> list[str]:
    """The command line to run on the SSH host. The plan or pipeline (with the plans and pre-launch files it names) is read here and
    sent to a private folder on the host, and the command names the sent copy. Unless `--remote-project` names the project, a host
    with a `projectsRoot` finds the project there, and the command names that folder instead of this machine's `--project-dir`."""
    raw = [item for item in _strip_host_options(arguments.raw_argv) if item != "--watch"]
    explicit = getattr(arguments, "remote_project", None) or os.environ.get("GDV_REMOTE_PROJECT")
    prep = _remote(entry, arguments)
    source_file = arguments.pipeline if arguments.command == "pipeline" else arguments.plan
    runs_in_editor = _request_for(pipeline, source_file.resolve().parent, project_dir).platform == "editor"  # the Editor opens a project
    needs_project = project_dir is not None or runs_in_editor or any(stage["stage"] == "build" for stage in pipeline["stages"])
    if getattr(arguments, "checkout", None):
        raw = submit.drop_option(raw, "--project-dir")  # the host resolves the managed checkout from its own host.json
    elif not explicit and entry.projects_root and needs_project:
        found = submit.find_project(prep, entry, submit.project_names(project_dir, pipeline["candidate"]["repo"]))
        raw = [*submit.drop_option(raw, "--project-dir"), "--project-dir", found]
    inputs = submit.Inputs()
    with tempfile.TemporaryDirectory(prefix="valtrion-send-") as staging:
        if arguments.command == "pipeline":
            original, name = arguments.pipeline, submit.pipeline_inputs(arguments.pipeline, pipeline, inputs, Path(staging))
        else:
            original, name = arguments.plan, inputs.add(arguments.plan.resolve())
            raw = submit.replace_prelaunch(raw, submit.prelaunch_names(raw, arguments.plan, inputs))
        inbox = submit.push_files(prep, entry, inputs.files)
    raw = submit.replace_positional(raw, original, submit.join_path(inbox, name, entry.os))
    return [*raw, "--inbox", submit.last_segment(inbox)]


def _start_remote_pipeline(arguments: argparse.Namespace, entry: config.HostEntry, extra: Sequence[str], pipeline: dict,
                           project_dir: Path | None) -> int:
    try:
        raw = _remote_command_line(arguments, entry, pipeline, project_dir)
    except submit.SubmitError as error:
        return _blocked(arguments.command, str(error))
    target = _remote(entry, arguments, retry=False)
    result = target.call([*raw, *extra], REMOTE_TIMEOUT_SEC)
    if not result.ok:
        return _blocked(arguments.command, f"host {entry.id} could not start the job: {(result.stderr or result.stdout).strip()[:300]}")
    sys.stdout.write(result.stdout)
    if arguments.watch:
        started = remote.extract_json_object(result.stdout) or {}
        if "jobId" not in started:
            return _blocked(arguments.command, f"host {entry.id} started the job but printed no job id to watch")
        arguments.job_id = started["jobId"]
        arguments.from_offset, arguments.stall_sec, arguments.poll_sec, arguments.unreachable_sec, arguments.no_collect = 0, None, 2.0, 1800.0, False
        return _job_watch(arguments, entry)
    return constants.EXIT_PASS


def _finite_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be a finite number of seconds, 0 or more")
    return value


def _common_run_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-dir", type=Path, help="Unity project folder (Unity version, git state, builds); default: GDV_UNITY_PROJECT or the project named by the nearest valtrion.json")
    parser.add_argument("--stall-sec", type=float, default=supervise.DEFAULT_STALL_SEC)
    parser.add_argument("--timeout-sec", type=float, default=7200)
    parser.add_argument("--lease-wait", type=float, default=None, metavar="SEC",
                        help="seconds to wait for a leased device or editor slot; default: leaseWaitSec of host.json, 0")
    parser.add_argument("--disk-wait", type=_finite_seconds, default=None, metavar="SEC",
                        help="seconds a build waits for free disk space (host.json disk.minFreeGiB) before it is BLOCKED; default: disk.waitSec of host.json, 0")
    parser.add_argument("--protect", action="append", default=[], help="path cleanup must never remove (repeatable)")
    parser.add_argument("--domain", choices=("auto", "user", "gui"), default="auto", help="macOS launchd domain (see `job start`)")
    parser.add_argument("--foreground", action="store_true",
                        help="run in this process, not in a launchd domain (debugging only; not for signing: over SSH it cannot reach the login keychain; cannot be combined with --domain user|gui)")
    parser.add_argument("--watch", action="store_true", help="follow the job to its end")
    parser.add_argument("--host", help="pin the host instead of choosing by capability")
    parser.add_argument("--remote-project")
    parser.add_argument("--checkout", metavar="ID", help="build in a managed checkout of the project (host.json projects.<candidate.repo>): "
                        "its id, or `auto` for the project's default; needs --sha, replaces --project-dir and --remote-project")
    parser.add_argument("--local", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--inbox", help=argparse.SUPPRESS)  # set by the caller of an SSH host: the folder name its files were sent to
    parser.add_argument("--selected-by", default=SELECTED_LOCAL, help=argparse.SUPPRESS)
    parser.add_argument("--transport", default="local", help=argparse.SUPPRESS)


def _configure_pipeline(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("pipeline", type=Path, help="a gdv.pipeline/1 file (read on this machine; sent to the host when it runs on an SSH host)")
    parser.add_argument("--sha", help="full commit under test; overrides candidate.sha and must be the commit that is checked out "
                                      "(otherwise the commit of the checkout is used and recorded)")
    _common_run_options(parser)


def _pipeline(arguments: argparse.Namespace) -> int:
    document, code = _load_document(arguments.pipeline, "pipeline")
    if document is None:
        return code
    if document.get("schema") != "gdv.pipeline/1":
        return _blocked("pipeline", f"{arguments.pipeline} is a {document.get('schema')}, not a gdv.pipeline/1")
    project_dir = None if getattr(arguments, "checkout", None) else _unity_project(arguments)
    payload = {"type": "pipeline", "file": str(arguments.pipeline.resolve()), "projectDir": str(project_dir) if project_dir else None,
               "baseDir": str(arguments.pipeline.resolve().parent)}
    if arguments.inbox and arguments.local and arguments.transport == "ssh":  # only a command forwarded by a caller carries an inbox
        payload["inbox"] = arguments.inbox
    if arguments.sha:
        if not re.fullmatch(r"[0-9a-f]{40}", arguments.sha):
            return _blocked("pipeline", "--sha must be the full 40-character commit")
        document["candidate"]["sha"] = arguments.sha
        payload["inline"] = document  # the pipeline as run: the file with the commit named on the command line
        payload["shaPinned"] = True
    elif getattr(arguments, "generated", False):
        payload["inline"] = document  # `valtrion build`: the file is a temporary copy, the job reads the pipeline itself
    try:
        return _start_pipeline(arguments, document, payload, arguments.pipeline.resolve().parent, project_dir, document["id"])
    except (jobs.JobError, config.ConfigError, ValueError) as error:
        return _blocked("pipeline", str(error))


def _configure_build(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sha", help="full commit under test; must be the commit that is checked out (default: the checkout's commit)")
    parser.add_argument("--profile", default="Internal")
    parser.add_argument("--platform", "--target", dest="platform", required=True, choices=("ios", "android", "windows", "macos", "editor"))
    parser.add_argument("--device-class", help="iOS signs for one device: the class to build for")
    parser.add_argument("--device-id", help="iOS signs for one device: the device to build for")
    parser.add_argument("--no-reuse", action="store_true", help="build even when a clean build of this commit, profile and toolchain is cached")
    parser.add_argument("--check-key", help="the check this build settles, as `valtrion select --json` lists it; its result has this plan.id "
                                           "(default: player:<platform>)")
    parser.add_argument("--clean", action="store_true", help="a managed checkout keeps its export, DerivedData and caches between builds; "
                                                              "build from nothing this time")
    _common_run_options(parser)


def _build(arguments: argparse.Namespace) -> int:
    """Build-only pipeline: one `build` stage, no device. The artifact is published (path, hash, key in `result.json`) and a later
    `valtrion pipeline` / `valtrion run --artifact-key KEY` installs it without building."""
    if arguments.check_key and not re.fullmatch(r"player:[A-Za-z0-9][A-Za-z0-9._:-]{0,190}", arguments.check_key):
        return _blocked("build", f"--check-key {arguments.check_key!r} must be a player check key such as player:windows (letters, digits and ._:-)")
    device = {k: v for k, v in (("class", arguments.device_class), ("id", arguments.device_id), ("platform", arguments.platform)) if v}
    stage: dict = {"stage": "build"}
    if arguments.no_reuse or arguments.clean or arguments.check_key:
        stage["args"] = {**({"reuse": False} if arguments.no_reuse else {}), **({"clean": True} if arguments.clean else {}),
                         **({"checkKey": arguments.check_key} if arguments.check_key else {})}
    document = {"schema": "gdv.pipeline/1", "id": core.safe_identifier(f"build-{arguments.repo}-{arguments.profile}"),
                "candidate": {"repo": arguments.repo, "profile": arguments.profile}, "device": device, "stages": [stage]}
    _, errors = validate_document(document)
    if errors:
        return _blocked("build", "; ".join(errors))
    own = ("--repo", "--profile", "--platform", "--target", "--device-class", "--device-id", "--check-key")
    forwarded = [item for item in arguments.raw_argv[1:] if item not in ("--no-reuse", "--clean")]
    for option in own:
        forwarded = submit.drop_option(forwarded, option)
    with tempfile.TemporaryDirectory(prefix="valtrion-build-") as staging:
        staged = Path(staging) / f"{document['id']}.pipeline.json"
        core.atomic_write_json(staged, document)
        arguments.command, arguments.pipeline, arguments.inbox, arguments.generated = "pipeline", staged, arguments.inbox, True
        arguments.raw_argv = ["pipeline", str(staged), *forwarded]
        return _pipeline(arguments)


def _configure_builds(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="the published builds of this host")
    show = sub.add_parser("show", help="one published build")
    show.add_argument("key")
    collect = sub.add_parser("gc", help="remove published builds that were not used for a while")
    collect.add_argument("--keep-days", type=float, help="default: host.json buildCache.keepDays, 14")
    collect.add_argument("--dirty-keep-days", type=float, help="default: host.json buildCache.dirtyKeepDays, 2")
    collect.add_argument("--max-entries", type=int, help="default: host.json buildCache.maxEntries, 20")
    collect.add_argument("--per-source", type=int, help="builds kept per project folder or checkout and platform; default: host.json buildCache.perSource, no limit")
    collect.add_argument("--dry-run", action="store_true")
    collect.add_argument("--protect", action="append", default=[], help="a build key that must stay (repeatable)")


def _builds(arguments: argparse.Namespace) -> int:
    root = buildcache.builds_root()
    if arguments.action == "list":
        _out(buildcache.describe_all(root))
        return constants.EXIT_PASS
    if arguments.action == "show":
        entry = buildcache.find(root, arguments.key)
        if entry is None:
            return _blocked("builds", f"no published build with key {arguments.key}")
        _out({**entry.describe(), "manifest": entry.manifest})
        return constants.EXIT_PASS
    try:
        limits = config.load_host_settings().build_cache.limits()
        for name, value in (("keep_days", arguments.keep_days), ("dirty_keep_days", arguments.dirty_keep_days),
                            ("max_entries", arguments.max_entries), ("per_source", arguments.per_source)):
            if value is not None:
                limits[name] = value
        report = buildcache.gc(root, dry_run=arguments.dry_run, protect=[*arguments.protect, *diskspace.active_build_keys()], **limits)
    except (config.ConfigError, ValueError) as error:
        return _blocked("builds", str(error))
    verb = "would remove" if arguments.dry_run else "removed"
    for item in report.removed:
        print(f"{verb} {item}")
    for key, reason in report.kept:
        print(f"kept {key}: {reason}")
    print(f"{verb} {len(report.removed)} build(s) from {root}")
    return constants.EXIT_PASS


def _configure_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("plan", type=Path, help="a gdv.plan/1 file")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sha", required=True, help="full commit sha under test")
    parser.add_argument("--profile", default="Internal")
    parser.add_argument("--app", required=True, help="application id to launch on the device")
    parser.add_argument("--device-class")
    parser.add_argument("--device-id")
    parser.add_argument("--platform", "--target", dest="platform", choices=("ios", "android", "windows", "macos", "editor"),
                        help="platform to run on; the host is chosen by capability (this machine first)")
    parser.add_argument("--artifact", help="install this build artifact before the run")
    parser.add_argument("--artifact-key", help="install the published build with this key (see `valtrion builds list`) before the run")
    parser.add_argument("--build", action="store_true", help="build the project first")
    parser.add_argument("--launch-arg", action="append", default=[])
    parser.add_argument("--launch-timeout", type=float, default=None, metavar="SEC",
                        help="how long the launch may wait for a slow start of the application (Android: after `am start` reports a "
                             "timeout), 1 to 300; default: launchTimeoutSec of host.json, 60")
    parser.add_argument("--prelaunch-file", action="append", default=[], metavar="LOCAL=REMOTE",
                        help="push LOCAL (relative to the plan file) to REMOTE (relative to the application's data root) before "
                             "the application starts; repeatable")
    _common_run_options(parser)


def _run(arguments: argparse.Namespace) -> int:
    document, code = _load_document(arguments.plan, "run")
    if document is None:
        return code
    if document.get("schema") != "gdv.plan/1":
        return _blocked("run", f"{arguments.plan} is a {document.get('schema')}, not a gdv.plan/1")
    plan_path = arguments.plan.resolve()
    device = {k: v for k, v in (("class", arguments.device_class), ("id", arguments.device_id), ("platform", arguments.platform)) if v}
    stages: list[dict] = []
    if arguments.build:
        stages.append({"stage": "build"})
    if arguments.artifact_key and (arguments.build or arguments.artifact):
        return _blocked("run", "--artifact-key cannot be combined with --build or --artifact")
    if arguments.build or arguments.artifact or arguments.artifact_key:
        install_args = {"artifact": arguments.artifact} if arguments.artifact and not arguments.build else \
            {"artifactKey": arguments.artifact_key} if arguments.artifact_key else {}
        stages.append({"stage": "install", **({"args": install_args} if install_args else {})})
    run_stage: dict = {"stage": "run", "plan": str(plan_path), "args": {"app": arguments.app, "launchArgs": arguments.launch_arg}}
    if arguments.launch_timeout is not None:
        try:
            run_stage["args"]["launchTimeoutSec"] = config.check_launch_timeout(arguments.launch_timeout, "--launch-timeout")
        except config.ConfigError as error:
            return _blocked("run", str(error))
    if arguments.prelaunch_file:
        pairs = [item.partition("=") for item in arguments.prelaunch_file]
        if any(not local or sep != "=" or not remote for local, sep, remote in pairs):
            return _blocked("run", "--prelaunch-file takes LOCAL=REMOTE")
        run_stage["prelaunchFiles"] = [{"local": local, "remote": remote} for local, _, remote in pairs]
    stages.append(run_stage)
    pipeline = {"schema": "gdv.pipeline/1", "id": core.safe_identifier(f"run-{document['id']}"),
                "candidate": {"repo": arguments.repo, "sha": arguments.sha, "profile": arguments.profile}, "device": device, "stages": stages}
    _, errors = validate_document(pipeline)
    if errors:
        return _blocked("run", "; ".join(errors))
    project_dir = None if getattr(arguments, "checkout", None) else _unity_project(arguments)
    payload = {"type": "pipeline", "file": "<inline>", "inline": pipeline, "projectDir": str(project_dir) if project_dir else None,
               "baseDir": str(plan_path.parent)}
    if arguments.inbox and arguments.local and arguments.transport == "ssh":  # only a command forwarded by a caller carries an inbox
        payload["inbox"] = arguments.inbox
    try:
        return _start_pipeline(arguments, pipeline, payload, plan_path.parent, project_dir, document["id"])
    except (jobs.JobError, config.ConfigError, ValueError) as error:
        return _blocked("run", str(error))


# -- collect, compare, baseline, gc -----------------------------------------------------------------------------------------


def _configure_collect(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("job_id")
    parser.add_argument("--host", required=True)
    parser.add_argument("--remote-project")


def _collect(arguments: argparse.Namespace) -> int:
    entry = _entry(arguments)
    if entry is None:
        return _blocked("collect", "the job is on this machine; nothing to collect")
    try:
        result = _remote(entry, arguments).call(["job", "tail", arguments.job_id, "--from", "0"], 60)
        info = remote.extract_json_object(result.stdout) if result.ok else None
    except ValueError as error:
        return _blocked("collect", str(error))
    if not info or not info.get("relDir"):
        return _blocked("collect", f"host {entry.id} did not describe job {arguments.job_id}: {(result.stderr or '').strip()[:200]}")
    root = config.load_host_settings().artifacts_root
    pulled = remote.pull_run(remote.make_transport(entry), info["dir"], root / info["relDir"])
    if not pulled.ok:
        return _blocked("collect", f"copy failed: {(pulled.stderr or '').strip()[:200]}")
    print(root / info["relDir"])
    return constants.EXIT_PASS


def _configure_compare(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("result", type=Path, help="a gdv.result/1 file")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--baselines", type=Path, required=True, help="the project's baselines folder")


def _read(path: Path, command: str):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")), None
    except (OSError, ValueError) as error:
        return None, _blocked(command, f"cannot read {path}: {error}")


def _compare(arguments: argparse.Namespace) -> int:
    result, failure = _read(arguments.result, "compare")
    if failure is not None:
        return failure
    plan, failure = _read(arguments.plan, "compare")
    if failure is not None:
        return failure
    try:
        verdict, shown = comparing.compare_against_folders(result, plan, [arguments.baselines.resolve()])
    except (KeyError, TypeError):
        return _blocked("compare", f"{arguments.result} is not a gdv.result/1")
    _out({"verdict": verdict.verdict, "baseline": shown, "details": verdict.details})
    return verdict.exit_code


def _configure_baseline(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="action", required=True)
    promote = sub.add_parser("promote", help="write a baseline from a passing run on a clean main commit")
    promote.add_argument("result", type=Path)
    promote.add_argument("--plan", type=Path, required=True)
    promote.add_argument("--baselines", type=Path, required=True)
    promote.add_argument("--repo-root", type=Path, required=True)
    promote.add_argument("--main-branch", default="main")


def _baseline(arguments: argparse.Namespace) -> int:
    result, failure = _read(arguments.result, "baseline")
    if failure is not None:
        return failure
    plan, failure = _read(arguments.plan, "baseline")
    if failure is not None:
        return failure
    try:
        path = comparing.promote(
            result, plan, arguments.baselines, arguments.repo_root, main_branch=arguments.main_branch,
            runner_state=lambda: doctor.probe_runner(core.local_os(), doctor.run_probe)["state"])
    except comparing.PromotionRefused as error:
        return _blocked("baseline", f"promotion refused: {error}")
    print(f"wrote {path}; commit it through an ordinary pull request")
    return constants.EXIT_PASS


def _configure_gc(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--keep-days", type=float, help="default: host.json retention.keepDays")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--protect", action="append", default=[])


def _gc(arguments: argparse.Namespace) -> int:
    try:
        settings = config.load_host_settings()
        days = settings.retention_days if arguments.keep_days is None else arguments.keep_days
        report = artifacts.gc(settings.artifacts_root, days, arguments.dry_run, [Path(p) for p in arguments.protect])
    except (config.ConfigError, ValueError) as error:
        return _blocked("gc", str(error))
    verb = "would remove" if arguments.dry_run else "removed"
    for path in report.removed:
        print(f"{verb} {path}")
    for path, reason in report.kept:
        print(f"kept {path}: {reason}")
    print(f"{verb} {len(report.removed)} run(s) older than {days:g} days from {settings.artifacts_root}")
    stale = workroot.sweep(settings.roots, jobs.active_job_ids(), dry_run=arguments.dry_run, keep=jobs.kept_work_reason)
    for path in stale:
        print(f"{verb} {path} (work root, abandoned by a runner that died)")
    for job_id in jobs.prune_index(settings.artifacts_root, dry_run=arguments.dry_run):  # after the run folders went: their index entries point nowhere
        print(f"{verb} job index entry {job_id}")
    built = buildcache.gc(buildcache.builds_root(), dry_run=arguments.dry_run, protect=diskspace.active_build_keys(),
                          **settings.build_cache.limits())
    for item in built.removed:
        print(f"{verb} build {item}")
    return constants.EXIT_PASS


def _configure_retain(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("job_id")


def _retain(arguments: argparse.Namespace) -> int:
    try:
        directory = jobs.find_job_directory(arguments.job_id)
    except jobs.JobError as error:
        return _blocked("retain", str(error))
    artifacts.mark_retained(directory)
    print(f"{directory} is retained; `valtrion gc` will not remove it")
    return constants.EXIT_PASS


def _configure_workroot(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="action", required=True)
    show = sub.add_parser("show", help="print the work root, its directories and any protected-folder warning")
    show.add_argument("--json", action="store_true")
    checkout = sub.add_parser("checkout", help="a detached checkout of a commit in the job's directory under the work root")
    checkout.add_argument("--project", required=True)
    checkout.add_argument("--sha", required=True, help="full 40-character commit hash")
    checkout.add_argument("--job-id", required=True, help="the job whose directory receives the checkout (GDV_JOB_ID)")
    checkout.add_argument("--source", help="URL or path to fetch the commit from; default: origin of --repo")
    checkout.add_argument("--repo", type=Path, help="a working copy whose origin URL is the source")
    sub.add_parser("inbox", help="create a private folder under the work root for files a caller sends to run a job here; prints its path as JSON")
    release = sub.add_parser("release", help="remove a job's directories under the work root")
    release.add_argument("--job-id", required=True)
    sweep = sub.add_parser("sweep", help="remove job directories left behind by a runner that died and expired idle scratch directories")
    sweep.add_argument("--max-age-hours", type=float, default=workroot.STALE_JOB_DIRECTORY_SEC / 3600,
                       help="idle time of an abandoned job directory (scratch directories use host.json scratch.maxAgeHours)")
    sweep.add_argument("--dry-run", action="store_true")
    sweep.add_argument("--any-owner", action="store_true",
                       help="also remove expired scratch directories of other owners of this host user (never another host user's)")
    scratch_parser = sub.add_parser("scratch", help="temporary copies for an agent under <work root>/scratch, with a marker and an expiry")
    actions = scratch_parser.add_subparsers(dest="scratch_action", required=True)
    create = actions.add_parser("create", help="make <work root>/scratch/<name>-<hex>/ (with --sha: a detached checkout in its checkout/); "
                                               "prints its path as JSON")
    create.add_argument("--name", required=True, help="1-40 letters, digits, '.', '_', '-'")
    create.add_argument("--project", help="the project whose mirror the checkout hangs off (with --sha)")
    create.add_argument("--sha", help="full 40-character commit hash to check out into checkout/")
    create.add_argument("--source", help="URL or path to fetch the commit from; default: origin of --repo")
    create.add_argument("--repo", type=Path, help="a working copy whose origin URL is the source")
    create.add_argument("--ttl-hours", type=float, help="lifetime; default host.json scratch.maxAgeHours (24)")
    create.add_argument("--job-id", help="a job the copy belongs to: the sweep may remove it once that job has ended")
    create.add_argument("--lease", help="a lease the copy depends on: while it is held the copy is never removed")
    release_scratch = actions.add_parser("release", help="remove exactly one scratch directory, when it is idle and yours")
    release_scratch.add_argument("id", help="the id `scratch create` printed")
    release_scratch.add_argument("--any-owner", action="store_true", help="release a scratch directory of another owner of this host user")
    release_scratch.add_argument("--dry-run", action="store_true")
    actions.add_parser("list", help="the scratch directories with owner and expiry, as JSON")
    adopt = sub.add_parser("adopt", help="make an unowned folder of the work root a scratch directory (moved into scratch/) so it expires")
    adopt.add_argument("path", type=Path, help="a direct entry of the work root (absolute, or its name)")
    adopt.add_argument("--yes", action="store_true", help="required: confirms that the owner wants valtrion to remove it once it expires")
    adopt.add_argument("--name", help="scratch name; default: the folder's name")
    adopt.add_argument("--ttl-hours", type=float, help="lifetime from now; default host.json scratch.maxAgeHours (24)")
    adopt.add_argument("--discard-unsaved-work", action="store_true",
                       help="adopt a git repository even with uncommitted changes, stashes or commits on no remote (lost when it expires)")


def _workroot(arguments: argparse.Namespace) -> int:
    try:
        settings = config.load_host_settings()
        roots = settings.roots
        if arguments.action == "show":
            warning = workroot.protected_location(settings.work_root)
            unowned = scratch.unowned(roots)
            data = {"workRoot": str(roots.root), "jobs": str(roots.jobs), "mirrors": str(roots.mirrors), "tools": str(roots.tools),
                    "launchd": str(roots.launchd), "scratch": str(roots.scratch), "protected": warning, **workroot.usage(roots),
                    "unowned": unowned, "warnings": [scratch.describe_unowned(item) for item in unowned]}
            if arguments.json:
                _out(data)
            else:
                for key, value in data.items():
                    if key not in ("unowned", "warnings"):
                        print(f"{key}: {value}")
                for line in data["warnings"]:
                    print(f"WARNING {line}")
            return constants.EXIT_PASS
        if arguments.action == "scratch":
            return _workroot_scratch(arguments, settings)
        if arguments.action == "adopt":
            if not arguments.yes:
                return _blocked("workroot", "adopt moves the folder into scratch/ and lets valtrion remove it once it expires; the owner "
                                            "confirms that with --yes")
            hours = settings.scratch_max_age_hours if arguments.ttl_hours is None else arguments.ttl_hours
            adopt_warnings: list[str] = []
            folder, marker = scratch.adopt(roots, arguments.path, scratch.caller(), max_age_hours=hours, name=arguments.name,
                                           discard_unsaved_work=arguments.discard_unsaved_work, warnings=adopt_warnings)
            data = {"scratch": str(folder), "id": marker["id"], "expiresUtc": marker["expiresUtc"], "adoptedFrom": marker["adoptedFrom"]}
            if adopt_warnings:
                data["warnings"] = adopt_warnings
            _out(data)
            return constants.EXIT_PASS
        if arguments.action == "inbox":
            folder = roots.inbox()
            _out({"inbox": str(folder)})
            return constants.EXIT_PASS
        if arguments.action == "checkout":
            source = arguments.source
            if not source:
                if arguments.repo is None:
                    return _blocked("workroot", "give --source or --repo")
                code, output = workroot.run_git(["remote", "get-url", "origin"], arguments.repo)
                if code != 0:
                    return _blocked("workroot", f"cannot read the origin of {arguments.repo}: {output[:200]}")
                source = output.strip()
            path = workroot.add_checkout(roots, arguments.project, source, arguments.sha, arguments.job_id)
            data = {"checkout": str(path), "jobDir": str(roots.job(arguments.job_id).base)}
            warnings = workroot.checkout_warnings(path)  # tracked files only: the Library a build adds comes later
            if warnings:
                data["warnings"] = warnings
            _out(data)
            return constants.EXIT_PASS
        if arguments.action == "release":
            _out({"removed": workroot.cleanup_job(roots, arguments.job_id)})
            return constants.EXIT_PASS
        active = jobs.active_job_ids()
        removed = workroot.sweep(roots, active, arguments.max_age_hours * 3600, arguments.dry_run, keep=jobs.kept_work_reason)
        swept = scratch.sweep(roots, scratch.caller(), active, settings.scratch_max_age_hours * 3600, any_owner=arguments.any_owner,
                              dry_run=arguments.dry_run)  # the folders of the active jobs are protected by the sweep itself
        removed.extend(swept.removed)
        verb = "would remove" if arguments.dry_run else "removed"
        for path in removed:
            print(f"{verb} {path}")
        for path, reason in swept.kept:
            print(f"kept {path}: {reason}")
        unowned = scratch.unowned(roots)
        for item in unowned:
            print(f"WARNING {scratch.describe_unowned(item)}")
        print(f"{len(removed)} abandoned item(s), {len(unowned)} unowned entr{'y' if len(unowned) == 1 else 'ies'} (never removed)")
        return constants.EXIT_PASS
    except (config.ConfigError, workroot.WorkRootError, scratch.ScratchError, OSError, ValueError) as error:
        return _blocked("workroot", str(error))  # ValueError: an invalid host.json leasesDir, read for the lease checks


def _workroot_scratch(arguments: argparse.Namespace, settings: config.HostSettings) -> int:
    roots = settings.roots
    if arguments.scratch_action == "list":
        _out({"scratch": str(roots.scratch), "directories": scratch.listing(roots)})
        return constants.EXIT_PASS
    if arguments.scratch_action == "release":
        path = scratch.release(roots, arguments.id, scratch.caller(), any_owner=arguments.any_owner, dry_run=arguments.dry_run)
        _out({"wouldRemove" if arguments.dry_run else "removed": [path]})
        return constants.EXIT_PASS
    source = arguments.source
    if arguments.sha and not source:
        if arguments.repo is None:
            return _blocked("workroot", "a scratch checkout (--sha) needs --source or --repo")
        code, output = workroot.run_git(["remote", "get-url", "origin"], arguments.repo)
        if code != 0:
            return _blocked("workroot", f"cannot read the origin of {arguments.repo}: {output[:200]}")
        source = output.strip()
    hours = settings.scratch_max_age_hours if arguments.ttl_hours is None else arguments.ttl_hours
    folder, marker = scratch.create(roots, arguments.name, scratch.caller(), max_age_hours=hours, project=arguments.project,
                                    source=source, sha=arguments.sha, job=arguments.job_id, lease_resource=arguments.lease)
    data = {"scratch": str(folder), "id": marker["id"], "expiresUtc": marker["expiresUtc"],
            "release": f"valtrion workroot scratch release {marker['id']}"}
    if arguments.sha:
        data["checkout"] = str(folder / scratch.CHECKOUT)
        warnings = workroot.checkout_warnings(folder / scratch.CHECKOUT)
        if warnings:
            data["warnings"] = warnings
    _out(data)
    return constants.EXIT_PASS


# -- unity ------------------------------------------------------------------------------------------------------------------

REMOTE_UNITY_NO_TIMEOUT_SEC = 24 * 3600
REMOTE_UNITY_MARGIN_SEC = 300
REMOTE_UNITY_LOST_HINT = ("; if the connection broke during the run its result is unknown: the run on the host is stopped (Unity killed, leases "
                          "released) when the host sees the end of its input, which could not be confirmed on a Windows host; otherwise it "
                          "runs on until Unity ends or --timeout-sec expires, and its leases are released then. See `valtrion lease list --host`")
STOP_ON_STDIN_EOF = "--stop-on-stdin-eof"


def _configure_unity(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run", help="run Unity in batch mode under the host's unity-project and unity-editor leases and the watchdog",
                         description="valtrion unity run [options] -- <Unity arguments>. The last stdout line is STATUS <name>; "
                                     "exit codes: PASS 0, FAIL 1, BLOCKED 2, STALL and TIMEOUT 3.")
    run.add_argument("--project-dir", type=Path, help="Unity project (when the Unity arguments carry no -projectPath); default: GDV_UNITY_PROJECT or valtrion.json")
    run.add_argument("--unity", help="path of the Unity executable (or a macOS .app) instead of the Editor of ProjectSettings/ProjectVersion.txt")
    run.add_argument("--lease-wait", type=_finite_seconds, default=None, metavar="SEC",
                     help="seconds to wait for the project and an Editor slot before BLOCKED (default: leaseWaitSec of host.json, 1800)")
    run.add_argument("--timeout-sec", type=_finite_seconds, default=0.0, metavar="N", help="hard timeout of Unity; 0 (default) = none")
    run.add_argument("--stall-sec", type=_finite_seconds, default=600.0, metavar="N",
                     help="STALL when the -logFile has not grown for N s (default 600; 0 = off; also off without -logFile)")
    run.add_argument("--purpose", default="", help="text recorded in the leases")
    run.add_argument("--lfs-materialize", action=argparse.BooleanOptionalAction, default=None,
                     help="turn Git LFS pointer files of the project into their content before Unity starts, from the host's shared LFS "
                          "cache, fetching only missing objects (default: lfs.materialize of host.json, on)")
    run.add_argument(STOP_ON_STDIN_EOF, action="store_true", help=argparse.SUPPRESS)  # added by a caller that keeps this command's input open
    _add_host_options(run)
    run.add_argument("unity_args", nargs=argparse.REMAINDER, help="after `--`: the arguments passed to Unity")


def _unity_arguments(arguments: argparse.Namespace) -> list[str]:
    raw = list(getattr(arguments, "raw_argv", None) or [])
    if "--" in raw:
        return raw[raw.index("--") + 1:]
    rest = list(arguments.unity_args)
    return rest[1:] if rest[:1] == ["--"] else rest


def _unity(arguments: argparse.Namespace) -> int:
    from . import unityrun

    entry = _entry(arguments)
    try:  # the remote host applies its own host.json when no wait was given; here the wait only sizes the transport timeout
        wait = _lease_wait(arguments.lease_wait, unityrun.DEFAULT_LEASE_WAIT_SEC)
    except config.ConfigError as error:
        return _blocked("unity", str(error))
    if entry is not None:
        # Starting Unity is not idempotent, so the transport does not retry; the output arrives when the run has ended.
        limit = arguments.timeout_sec if arguments.timeout_sec > 0 else REMOTE_UNITY_NO_TIMEOUT_SEC
        return _forward(arguments, entry, timeout=int(wait + limit + REMOTE_UNITY_MARGIN_SEC), retry=False,
                        lost_hint=REMOTE_UNITY_LOST_HINT, lifeline=True, extra=(STOP_ON_STDIN_EOF,))

    unity_args = _unity_arguments(arguments)
    found = None
    if unityrun.option_value(unity_args, "-projectPath") is None:  # -projectPath wins; a broken valtrion.json must not block such a run
        try:
            found = project.resolve_unity_project(arguments.project_dir)
        except project.ProjectError as error:
            return _blocked("unity", str(error))
    return unityrun.run(unity_args, found or arguments.project_dir, arguments.unity, wait, arguments.timeout_sec,
                        arguments.stall_sec, arguments.purpose, lifeline=sys.stdin.fileno() if arguments.stop_on_stdin_eof else None,
                        lfs_materialize=arguments.lfs_materialize)


def register_all() -> None:
    table: list[tuple[str, str, Callable, Callable]] = [
        ("hosts", "list or add hosts", _hosts, _configure_hosts),
        ("doctor", "probe what this host can do (read-only)", _doctor, _configure_doctor),
        ("devices", "list or refresh attached devices", _devices, _configure_devices),
        ("lease", "acquire, renew, release and inspect leases", _lease, _configure_lease),
        ("job", "start, watch, inspect and cancel detached jobs", _job, _configure_job),
        ("unity", "run Unity in batch mode under Valtrion leases and supervision", _unity, _configure_unity),
        ("pipeline", "run a pipeline as a detached job on a host chosen by capability", _pipeline, _configure_pipeline),
        ("build", "build the project without a device and publish the artifact", _build, _configure_build),
        ("builds", "list, show and remove the published builds of this host", _builds, _configure_builds),
        ("run", "run one plan on a device as a detached job", _run, _configure_run),
        ("collect", "pull a remote job's artifacts into the local artifacts root", _collect, _configure_collect),
        ("compare", "compare a result with its baseline", _compare, _configure_compare),
        ("baseline", "promote a run to a baseline", _baseline, _configure_baseline),
        ("gc", "remove old run folders", _gc, _configure_gc),
        ("workroot", "the work root for builds and checkouts outside protected folders", _workroot, _configure_workroot),
        ("retain", "keep a run from `valtrion gc`", _retain, _configure_retain),
    ]
    for name, help_text, handler, configure in table:
        register(Command(name, help_text, "host core", handler, configure))
