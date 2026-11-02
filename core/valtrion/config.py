"""Machine-local configuration under the valtrion home: host inventory, host settings and the device registry.

`hosts.json` (`gdv.hosts/1`) lists where work may run; the local machine is added automatically when it is not listed.
`host.json` holds this machine's own settings (artifacts root, retention). `registry.json` (`gdv.registry/1`) holds the devices
discovered on this host; discovery refreshes it, only aliases, classes and notes are edited by hand and survive a refresh.
"""

from __future__ import annotations

import math
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
class ManagedCheckout:
    id: str
    path: Path  # the root of the git checkout


@dataclass(frozen=True)
class ManagedProject:
    """`host.json` `projects.<name>`: a small pool of build checkouts valtrion owns, and the remote it can create them from."""
    name: str
    checkouts: tuple[ManagedCheckout, ...]
    remote: str | None = None
    unity_path: str = "."  # the Unity project below the checkout root
    default: str = ""  # id of the checkout used when a job names none

    def pick(self, checkout_id: str | None) -> ManagedCheckout | None:
        wanted = checkout_id or self.default or self.checkouts[0].id
        return next((item for item in self.checkouts if item.id == wanted), None)

    def project_dir(self, item: ManagedCheckout) -> Path:
        return item.path if self.unity_path in ("", ".") else item.path / self.unity_path


@dataclass(frozen=True)
class UnityAccelerator:
    """`host.json` `unityAccelerator`: a Unity Accelerator (cache server) for imported assets, given to Unity on the command line."""
    endpoint: str  # host:port
    namespace_prefix: str | None = None
    download: bool = True
    upload: bool = True

    def unity_args(self) -> list[str]:
        """The switches of a Unity batch-mode command line; batch mode does not reliably take them from the Editor preferences."""
        argv = ["-EnableCacheServer", "-cacheServerEndpoint", self.endpoint]
        if self.namespace_prefix:
            argv += ["-cacheServerNamespacePrefix", self.namespace_prefix]
        return argv + ["-cacheServerEnableDownload", "true" if self.download else "false",
                       "-cacheServerEnableUpload", "true" if self.upload else "false"]


_ENDPOINT = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\]):([0-9]{1,5})")
_NAMESPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _load_unity_accelerator(value: Any) -> UnityAccelerator | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError("host.json: unityAccelerator must be an object")
    unknown = sorted(set(value) - {"endpoint", "namespacePrefix", "download", "upload"})
    if unknown:
        raise ConfigError(f"host.json: unityAccelerator has unknown keys: {', '.join(unknown)}")
    endpoint = value.get("endpoint")
    match = _ENDPOINT.fullmatch(endpoint) if isinstance(endpoint, str) else None
    if match is None or not 1 <= int(match.group(1)) <= 65535:
        raise ConfigError("host.json: unityAccelerator.endpoint must be host:port (for example 127.0.0.1:10080)")
    prefix = value.get("namespacePrefix")
    if prefix is not None and (not isinstance(prefix, str) or not _NAMESPACE.fullmatch(prefix)):
        raise ConfigError("host.json: unityAccelerator.namespacePrefix must be 1-64 letters, digits, '.', '_' or '-'")
    for key in ("download", "upload"):
        if not isinstance(value.get(key, True), bool):
            raise ConfigError(f"host.json: unityAccelerator.{key} must be true or false")
    return UnityAccelerator(endpoint, prefix, value.get("download", True), value.get("upload", True))


def unity_accelerator_args(home: Path | None = None) -> list[str]:
    """The cache-server switches for a Unity batch build on this host (empty when `host.json` names no `unityAccelerator`)."""
    accelerator = load_host_settings(home).unity_accelerator
    return accelerator.unity_args() if accelerator is not None else []


@dataclass(frozen=True)
class HostSettings:
    artifacts_root: Path
    retention_days: float = DEFAULT_RETENTION_DAYS
    work_root: Path = field(default_factory=workroot.default_work_root)
    #: `disk.minFreeGiB`: free space the work root's volume must have before a build; None (the default) turns the check off.
    min_free_gib: float | None = None
    #: `disk.keepDaysUnderPressure`: run folders older than this are removed when space is low (instead of `retention.keepDays`).
    keep_days_under_pressure: float = 2.0
    #: `projects`: the projects whose build checkouts valtrion owns (see `checkout.py`).
    projects: tuple["ManagedProject", ...] = ()
    #: `disk.waitSec`: how long a build waits for free space before it is BLOCKED (0, the default, blocks at once). `--disk-wait` wins.
    disk_wait_sec: float = 0.0
    #: Where the staged CLI copies of background jobs live (`<valtrion home>/tools`); None keeps them in the work root.
    tools_root: Path | None = None
    #: `unityAccelerator`: the cache server every Unity batch build of the platform adapters uses; None (the default) uses none.
    unity_accelerator: "UnityAccelerator | None" = None

    def project(self, name: str) -> "ManagedProject | None":
        return next((item for item in self.projects if item.name == name), None)

    @property
    def roots(self) -> workroot.WorkRoots:
        return workroot.WorkRoots(self.work_root, self.tools_root)


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
        return HostSettings(default_root, work_root=default_work, tools_root=home / "tools")
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
    disk = document.get("disk", {})
    if not isinstance(disk, dict):
        raise ConfigError("host.json: disk must be an object")
    minimum = disk.get("minFreeGiB")
    if minimum is not None and (isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or not math.isfinite(minimum) or minimum <= 0):
        raise ConfigError("host.json: disk.minFreeGiB must be a positive number")
    pressure_days = disk.get("keepDaysUnderPressure", 2.0)
    if isinstance(pressure_days, bool) or not isinstance(pressure_days, (int, float)) or not math.isfinite(pressure_days) or pressure_days < 0:
        raise ConfigError("host.json: disk.keepDaysUnderPressure must be a non-negative number")
    wait_sec = disk.get("waitSec", 0)
    if isinstance(wait_sec, bool) or not isinstance(wait_sec, (int, float)) or not math.isfinite(wait_sec) or wait_sec < 0:
        raise ConfigError("host.json: disk.waitSec must be a non-negative number")
    projects = _load_projects(document.get("projects", {}))
    return HostSettings(Path(root).expanduser() if root else default_root, float(days),
                        Path(work).expanduser() if work else default_work, float(minimum) if minimum is not None else None,
                        float(pressure_days), projects, float(wait_sec), tools_root=home / "tools",
                        unity_accelerator=_load_unity_accelerator(document.get("unityAccelerator")))


def _load_projects(value: Any) -> tuple[ManagedProject, ...]:
    if not isinstance(value, dict):
        raise ConfigError("host.json: projects must be an object")
    found: list[ManagedProject] = []
    for name, item in value.items():
        where = f"host.json: projects.{name}"
        if not isinstance(item, dict) or not isinstance(item.get("checkouts"), list) or not item["checkouts"]:
            raise ConfigError(f"{where} must be an object with a non-empty checkouts list")
        checkouts: list[ManagedCheckout] = []
        for entry in item["checkouts"]:
            identifier = entry.get("id") if isinstance(entry, dict) else None
            path = entry.get("path") if isinstance(entry, dict) else None
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", identifier) or identifier == "auto":
                raise ConfigError(f"{where}.checkouts: every entry needs an id of letters, digits, '.', '_' and '-'")
            if not isinstance(path, str) or not path or not Path(path).expanduser().is_absolute():
                raise ConfigError(f"{where}.checkouts[{identifier}].path must be an absolute path (or start with ~)")
            if any(existing.id == identifier for existing in checkouts):
                raise ConfigError(f"{where}.checkouts: the id {identifier} is used twice")
            checkouts.append(ManagedCheckout(identifier, Path(path).expanduser()))
        remote = item.get("remote")
        if remote is not None and (not isinstance(remote, str) or not remote):
            raise ConfigError(f"{where}.remote must be a non-empty string")
        if remote is not None and (remote.startswith("-") or remote.lower().startswith("ext::")):
            raise ConfigError(f"{where}.remote must be a git URL or path, not an option or an ext:: transport")
        unity_path = item.get("unityPath", ".")
        if (not isinstance(unity_path, str) or Path(unity_path).is_absolute() or ".." in Path(unity_path).parts
                or Path(unity_path).drive or Path(unity_path).root or unity_path.startswith(("/", "\\"))):
            raise ConfigError(f"{where}.unityPath must be a relative path inside the checkout")
        default = item.get("default", checkouts[0].id)
        if default not in {c.id for c in checkouts}:
            raise ConfigError(f"{where}.default must be the id of one of its checkouts")
        found.append(ManagedProject(name, tuple(checkouts), remote, unity_path, default))
    seen: list[tuple[Path, str]] = []
    for project in found:
        for entry in project.checkouts:
            here = Path(os.path.normcase(os.path.abspath(entry.path.expanduser())))
            for other, label in seen:
                if here == other or other in here.parents or here in other.parents:
                    raise ConfigError(f"host.json: the checkout {project.name}/{entry.id} ({entry.path}) is the same folder as, or lies inside "
                                      f"or around, {label}; managed checkouts must not overlap")
            seen.append((here, f"{project.name}/{entry.id}"))
    return tuple(found)


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
