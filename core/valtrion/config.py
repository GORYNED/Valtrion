"""Machine-local configuration under the valtrion home: host inventory, host settings and the device registry.

`hosts.json` (`gdv.hosts/1`) lists where work may run; the local machine is added automatically when it is not listed.
`host.json` holds this machine's own settings (artifacts root, retention). `registry.json` (`gdv.registry/1`) holds the devices
discovered on this host; discovery refreshes it, only aliases, classes and notes are edited by hand and survive a refresh.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

from . import core, workroot
from .adapter import DeviceInfo
from .resolver import HostInfo
from .schema import Validator, validate_document

DEFAULT_RETENTION_DAYS = 30


class ConfigError(Exception):
    """A configuration file is unreadable or violates its contract."""


@dataclass(frozen=True)
class HostEntry:
    id: str
    kind: str  # local or ssh
    os: str
    artifacts_root: Path
    ssh_target: str | None = None
    connect_timeout_sec: int = 10
    roles: tuple[str, ...] = ()
    projects_root: str | None = None
    worktree_policy: str | None = None
    declared: dict = field(default_factory=dict)
    auto: bool = False  # synthesized, not read from hosts.json
    valtrion_command: str | None = None  # absolute path of the valtrion command on an SSH host
    python: str | None = None  # absolute path of the Python (3.10+) hosts sync uses on an SSH host
    artifacts_root_text: str | None = None  # artifactsRoot exactly as written; a remote POSIX path must not be shown through this machine's Path

    def info(self) -> HostInfo:
        return HostInfo(self.id, self.kind, self.os, self.ssh_target, self.connect_timeout_sec)


@dataclass(frozen=True)
class HostSettings:
    artifacts_root: Path
    retention_days: float = DEFAULT_RETENTION_DAYS
    work_root: Path = field(default_factory=workroot.default_work_root)

    @property
    def roots(self) -> workroot.WorkRoots:
        return workroot.WorkRoots(self.work_root)


def hosts_path(home: Path | None = None) -> Path:
    return (home or core.valtrion_home()) / "hosts.json"


def host_json_path(home: Path | None = None) -> Path:
    return (home or core.valtrion_home()) / "host.json"


def registry_path(home: Path | None = None) -> Path:
    return (home or core.valtrion_home()) / "registry.json"


def _read_document(path: Path, validator: Validator | None) -> Any | None:
    try:
        document = core.read_json(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise ConfigError(f"cannot read {path}: {error}") from error
    schema_id, errors = validate_document(document, validator)
    if errors:
        raise ConfigError(f"{path} is not a valid {schema_id}: " + "; ".join(errors))
    return document


def load_host_settings(home: Path | None = None) -> HostSettings:
    """Host settings from host.json; `GDV_WORK_ROOT` overrides the work root for this process (it must be absolute)."""
    settings = _load_host_settings(home)
    override = os.environ.get(workroot.WORK_ROOT_ENV)
    if not override:
        return settings
    path = Path(override).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"{workroot.WORK_ROOT_ENV} must be an absolute path (or start with ~)")
    return replace(settings, work_root=path)


def _load_host_settings(home: Path | None = None) -> HostSettings:
    home = home or core.valtrion_home()
    default_root = home / "artifacts"
    default_work = _default_work_root(home)
    try:
        document = core.read_json(host_json_path(home))
    except FileNotFoundError:
        return HostSettings(default_root, work_root=default_work)
    except (OSError, ValueError) as error:
        raise ConfigError(f"cannot read {host_json_path(home)}: {error}") from error
    if not isinstance(document, dict):
        raise ConfigError(f"{host_json_path(home)} must be an object")
    root = document.get("artifactsRoot")
    if root is not None and (not isinstance(root, str) or not root):
        raise ConfigError("host.json: artifactsRoot must be a non-empty string")
    work = document.get("workRoot")
    if work is not None and (not isinstance(work, str) or not work):
        raise ConfigError("host.json: workRoot must be a non-empty string")
    if work is not None and not Path(work).expanduser().is_absolute():
        raise ConfigError("host.json: workRoot must be an absolute path (or start with ~)")
    retention = document.get("retention", {})
    days = retention.get("keepDays", DEFAULT_RETENTION_DAYS) if isinstance(retention, dict) else DEFAULT_RETENTION_DAYS
    if isinstance(days, bool) or not isinstance(days, (int, float)) or days < 0:
        raise ConfigError("host.json: retention.keepDays must be a non-negative number")
    return HostSettings(Path(root).expanduser() if root else default_root, float(days),
                        Path(work).expanduser() if work else default_work)


def _default_work_root(home: Path) -> Path:
    """An explicit valtrion home (tests, scratch runs) keeps its work root inside it; the real home uses the platform default."""
    if home != core.valtrion_home():
        return home / "work"
    return workroot.default_work_root()


def local_entry(home: Path | None = None) -> HostEntry:
    settings = load_host_settings(home)
    return HostEntry(core.local_host_id(), "local", core.local_os(), settings.artifacts_root, auto=True)


def load_hosts(home: Path | None = None, validator: Validator | None = None) -> list[HostEntry]:
    """Configured hosts plus the local machine. A configured local entry wins; otherwise one is synthesized."""
    document = _read_document(hosts_path(home), validator)
    entries: list[HostEntry] = []
    for item in (document or {}).get("hosts", []):
        transport = item["transport"]
        entries.append(HostEntry(
            id=item["id"], kind=transport["kind"], os=item["os"], artifacts_root=Path(item["artifactsRoot"]), artifacts_root_text=item["artifactsRoot"],
            ssh_target=transport.get("sshTarget"), connect_timeout_sec=int(transport.get("connectTimeoutSec", 10)),
            roles=tuple(item.get("roles", ())), projects_root=item.get("projectsRoot"), worktree_policy=item.get("worktreePolicy"),
            declared=item.get("capabilities", {}), valtrion_command=item.get("valtrionCommand"), python=item.get("python")))
    if not any(entry.kind == "local" for entry in entries):
        entries.insert(0, local_entry(home))
    return entries


def find_host(host_id: str, home: Path | None = None) -> HostEntry:
    for entry in load_hosts(home):
        if entry.id == host_id:
            return entry
    raise ConfigError(f"host {host_id!r} is not configured (see `valtrion hosts`)")


def local_host(home: Path | None = None) -> HostEntry:
    return next(entry for entry in load_hosts(home) if entry.kind == "local")


def projects_root_problem(root: str, os_name: str) -> str | None:
    """Why `root` cannot be a projects root on a host of `os_name`: it must be absolute on the host (or `~/...` on a POSIX host),
    and not a path that the local shell rewrote on its way here (MSYS path conversion)."""
    from .remote import remote_path_problem
    rewritten = remote_path_problem(root, os_name)
    if rewritten:
        return rewritten.replace("the remote path", "the projects root").replace("set GDV_REMOTE_PROJECT or use a script", "pass it from a script")
    if os_name == "windows":
        ok = bool(re.match(r"^[A-Za-z]:[\\/]", root)) or root.startswith("\\\\")
    else:
        ok = root.startswith("/") or root == "~" or root.startswith("~/")
    return None if ok else f"the projects root {root!r} must be an absolute path on the host" + ("" if os_name == "windows" else " (or start with ~/)")


def add_ssh_host(
        host_id: str, ssh_target: str, os_name: str, artifacts_root: str, home: Path | None = None, projects_root: str | None = None,
        connect_timeout_sec: int | None = None, python: str | None = None) -> Path:
    """Add or replace an SSH host in hosts.json, keeping the other entries; the file is validated before it is written."""
    if projects_root:
        problem = projects_root_problem(projects_root, os_name)
        if problem:
            raise ConfigError(problem)
    path = hosts_path(home)
    document = _read_document(path, None) or {"schema": "gdv.hosts/1", "hosts": []}
    hosts = [item for item in document["hosts"] if item["id"] != host_id]
    transport: dict[str, Any] = {"kind": "ssh", "sshTarget": ssh_target}
    if connect_timeout_sec is not None:
        transport["connectTimeoutSec"] = connect_timeout_sec
    item: dict[str, Any] = {"id": host_id, "transport": transport, "os": os_name, "artifactsRoot": artifacts_root}
    if projects_root:
        item["projectsRoot"] = projects_root
    if python:
        item["python"] = python
    hosts.append(item)
    updated = {"schema": "gdv.hosts/1", "hosts": hosts}
    _, errors = validate_document(updated)
    if errors:
        raise ConfigError("the host would be invalid: " + "; ".join(errors))
    core.atomic_write_json(path, updated)
    return path


def set_host_command(host_id: str, command: str, python: str | None = None, *, home: Path | None = None) -> Path:
    """Record the absolute path of the valtrion command (and the Python that installed it) of an SSH host in hosts.json."""
    path = hosts_path(home)
    document = _read_document(path, None)
    hosts = (document or {}).get("hosts", [])
    for item in hosts:
        if item["id"] == host_id:
            item["valtrionCommand"] = command
            if python:
                item["python"] = python
            break
    else:
        raise ConfigError(f"host {host_id!r} is not in {path}")
    _, errors = validate_document(document)
    if errors:
        raise ConfigError("the host would be invalid: " + "; ".join(errors))
    core.atomic_write_json(path, document)
    return path


# -- registry ---------------------------------------------------------------------------------------------------------------


def load_registry(home: Path | None = None, validator: Validator | None = None) -> dict:
    document = _read_document(registry_path(home), validator)
    if document is not None:
        return document
    entry = local_entry(home)
    return {"schema": "gdv.registry/1", "host": {"id": entry.id, "os": entry.os, "roles": []}, "devices": []}


def merge_discovered(registry: dict, devices: Sequence[DeviceInfo], platforms: Sequence[str], host_id: str) -> dict:
    """Replace the devices of the given platforms with the discovered ones, keeping alias, class override and notes by id.
    A device of another platform stays untouched, and a registered device that is no longer discovered is dropped
    (a stale entry is never trusted over discovery)."""
    previous = {item["id"]: item for item in registry.get("devices", [])}
    kept = [item for item in registry.get("devices", []) if item["platform"] not in platforms]
    for device in devices:
        before = previous.get(device.id, {})
        item: dict[str, Any] = {
            "id": device.id, "hostId": host_id, "platform": device.platform, "class": before.get("class", device.device_class),
            "evidence": device.evidence, "capabilities": sorted(set(device.capabilities) | set(before.get("capabilities", []))),
            "exclusive": device.exclusive}
        if device.model:
            item["model"] = device.model
        if device.os_version:
            item["os"] = device.os_version
        for optional in ("alias", "notes"):
            if optional in before:
                item[optional] = before[optional]
        kept.append(item)
    return {**registry, "devices": sorted(kept, key=lambda item: item["id"])}


def save_registry(registry: dict, home: Path | None = None) -> Path:
    _, errors = validate_document(registry)
    if errors:
        raise ConfigError("the registry would be invalid: " + "; ".join(errors))
    path = registry_path(home)
    core.atomic_write_json(path, registry)
    return path


def registry_devices(registry: dict) -> list[DeviceInfo]:
    result = []
    for item in registry.get("devices", []):
        result.append(DeviceInfo(
            item["id"], item["platform"], item["class"], item.get("evidence", "physical"), item.get("model", ""),
            item.get("os", ""), tuple(item.get("capabilities", ())), item.get("exclusive", True)))
    return result
