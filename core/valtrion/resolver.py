"""Chooses the host that runs a request, deterministically.

The local machine is probed first; if it satisfies the whole request the run is local and no transport is involved. Otherwise the
first remote host (in id order) that satisfies it is used. A request no host satisfies is BLOCKED with one reason per host, never
a guess. The choice is recorded in the result as `host.id`, `host.transport` and `host.selectedBy`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from . import constants
from .adapter import DeviceInfo

SELECTED_LOCAL = "local"
SELECTED_CAPABILITY = "capability"
SELECTED_PINNED = "pinned"


@dataclass(frozen=True)
class HostCapabilities:
    """What a host can do right now: the result of `valtrion doctor` on it, with attached devices from discovery."""

    unity_editors: dict[str, tuple[str, ...]] = field(default_factory=dict)  # Editor version -> installed modules
    build_platforms: tuple[str, ...] = ()
    xcode: bool = False
    ios_signing: bool = False
    adb: bool = False
    devices: tuple[DeviceInfo, ...] = ()


@dataclass(frozen=True)
class HostInfo:
    id: str
    transport_kind: str  # "local" or "ssh"
    os: str
    ssh_target: str | None = None
    connect_timeout_sec: int = 10

    def __post_init__(self) -> None:
        if self.transport_kind not in ("local", "ssh"):
            raise ValueError(f"unknown transport kind: {self.transport_kind!r}")
        if self.transport_kind == "ssh" and not self.ssh_target:
            raise ValueError(f"ssh host {self.id!r} needs a target")


@dataclass(frozen=True)
class Request:
    """What a run needs. A build need and a device need are independent: a plan may need only one."""

    platform: str
    build: bool = False
    unity_version: str | None = None
    device: bool = False
    device_class: str | None = None
    device_capabilities: tuple[str, ...] = ()
    pinned_host: str | None = None


@dataclass(frozen=True)
class Resolution:
    status: str  # PASS (a host was chosen) or BLOCKED
    host: HostInfo | None = None
    selected_by: str | None = None
    reasons: tuple[str, ...] = ()

    @property
    def transport(self) -> str | None:
        return self.host.transport_kind if self.host else None

    @property
    def exit_code(self) -> int:
        return constants.exit_code_for_status(self.status)


@dataclass(frozen=True)
class HostUnavailable:
    """A probe's answer when a host cannot be used, with the reason (evidence included) in the words the resolver reports for it."""

    reason: str


#: Gives the capabilities of a host. None means the host cannot be reached (reported as `unreachable`); a `HostUnavailable` carries
#: a more precise reason (for example: reachable, but the valtrion command is missing there).
Probe = Callable[[HostInfo], "HostCapabilities | HostUnavailable | None"]


def missing(request: Request, capabilities: HostCapabilities) -> list[str]:
    """Reasons the host cannot serve the request; empty means it can."""
    reasons: list[str] = []
    if request.build:
        if request.platform not in capabilities.build_platforms:
            reasons.append(f"cannot build {request.platform}")
        if request.unity_version is not None and request.unity_version not in capabilities.unity_editors:
            reasons.append(f"Unity {request.unity_version} is not installed")
        if request.platform == "ios":
            if not capabilities.xcode:
                reasons.append("Xcode is missing")
            if not capabilities.ios_signing:
                reasons.append("iOS signing is not available (no authorized GUI session)")
        if request.platform == "android" and not capabilities.adb:
            reasons.append("adb is missing")
    if request.device:
        candidates = [d for d in capabilities.devices if d.platform == request.platform]
        if request.device_class is not None:
            candidates = [d for d in candidates if d.device_class == request.device_class]
        candidates = [d for d in candidates if set(request.device_capabilities) <= set(d.capabilities)]
        if not candidates:
            wanted = request.device_class or request.platform
            extra = f" with {', '.join(request.device_capabilities)}" if request.device_capabilities else ""
            reasons.append(f"no attached {wanted} device{extra}")
    return reasons


class HostResolver:
    """Local first, then remote hosts in id order. Selection depends only on the request and the probed capabilities."""

    def __init__(self, local: HostInfo, remotes: Iterable[HostInfo], probe: Probe) -> None:
        if local.transport_kind != "local":
            raise ValueError("the first host must be the local machine")
        remote_list = sorted(remotes, key=lambda host: host.id)
        identifiers = [local.id, *(host.id for host in remote_list)]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("host ids must be unique")
        if any(host.transport_kind == "local" for host in remote_list):
            raise ValueError("only one host can be local")
        self.local = local
        self.remotes: Sequence[HostInfo] = remote_list
        self._probe = probe

    @property
    def hosts(self) -> list[HostInfo]:
        return [self.local, *self.remotes]

    def resolve(self, request: Request) -> Resolution:
        if request.pinned_host is not None:
            return self._resolve_pinned(request)
        reasons: list[str] = []
        for host in self.hosts:
            outcome = self._check(host, request)
            if outcome is None:
                selected = SELECTED_LOCAL if host.transport_kind == "local" else SELECTED_CAPABILITY
                return Resolution(constants.PASS, host, selected)
            reasons.append(outcome)
        return Resolution(constants.BLOCKED, None, None, tuple(reasons))

    def _resolve_pinned(self, request: Request) -> Resolution:
        host = next((h for h in self.hosts if h.id == request.pinned_host), None)
        if host is None:
            return Resolution(constants.BLOCKED, None, None, (f"pinned host {request.pinned_host!r} is not configured",))
        outcome = self._check(host, request)
        if outcome is not None:
            return Resolution(constants.BLOCKED, None, None, (outcome,))
        return Resolution(constants.PASS, host, SELECTED_PINNED)

    def _check(self, host: HostInfo, request: Request) -> str | None:
        capabilities = self._probe(host)
        if capabilities is None:
            return f"{host.id}: unreachable"
        if isinstance(capabilities, HostUnavailable):
            return f"{host.id}: {capabilities.reason}"
        reasons = missing(request, capabilities)
        return f"{host.id}: {'; '.join(reasons)}" if reasons else None
