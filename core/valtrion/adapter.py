"""The platform adapter interface that iOS, Android, Windows, macOS and Editor adapters implement.

Adapters turn a platform into the same small set of operations. Every operation reports a `StepOutcome` with a status from the
contract; an adapter never raises for an expected condition such as a locked device or a missing tool, it returns BLOCKED or
NEEDS_PERMISSION with the action the owner must take.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from . import constants


@dataclass(frozen=True)
class StepOutcome:
    """Result of one adapter operation. `reason` is required for every status except PASS."""

    status: str
    reason: str | None = None
    artifacts: tuple[Path, ...] = ()
    details: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in constants.STATUSES:
            raise ValueError(f"unknown status: {self.status!r}")
        if self.status != constants.PASS and not self.reason:
            raise ValueError(f"status {self.status} requires a reason")

    @property
    def ok(self) -> bool:
        return self.status == constants.PASS


@dataclass(frozen=True)
class Capabilities:
    """What a host can do for one platform. Each missing prerequisite is listed in `blocked` with the reason."""

    platform: str
    available: tuple[str, ...] = ()
    blocked: Mapping[str, str] = field(default_factory=dict)

    def can(self, capability: str) -> bool:
        return capability in self.available and capability not in self.blocked


@dataclass(frozen=True)
class DeviceInfo:
    """A discovered target. `evidence` is what a result from this device may claim (physical, emulator, editor, host)."""

    id: str
    platform: str
    device_class: str
    evidence: str
    model: str = ""
    os_version: str = ""
    capabilities: tuple[str, ...] = ()
    exclusive: bool = True

    def __post_init__(self) -> None:
        if self.evidence not in constants.EVIDENCE_KINDS:
            raise ValueError(f"unknown evidence kind: {self.evidence!r}")

    @property
    def os_major(self) -> int:
        # The first token that starts with digits: "iPadOS 27.2" -> 27, "17.2 (21C62)" -> 17, "14" -> 14.
        for token in self.os_version.split():
            digits = ""
            for character in token:
                if character not in "0123456789":
                    break
                digits += character
            if digits:
                major = int(digits[:12])
                return major if major <= 2_147_483_647 else 0  # the result schema caps osMajor at int32
        return 0


@dataclass(frozen=True)
class DeviceHealth:
    """Condition of a device before and after a run. Unknown values stay None; they are recorded, never guessed."""

    reachable: bool
    locked: bool | None = None
    thermal_state: str | None = None
    battery_percent: int | None = None
    free_space_mb: int | None = None
    notes: tuple[str, ...] = ()
    #: Set when the device is attached but needs an owner action first (for example an unauthorized adb connection).
    #: The run is NEEDS_PERMISSION with this text, not BLOCKED.
    permission_needed: str | None = None


class PlatformAdapter(abc.ABC):
    """Operations every platform provides. Methods are synchronous and bounded by the supervisor's timeouts."""

    #: Platform id from the contract: ios, android, windows, macos or editor.
    platform: str

    def bind_application(self, app: str) -> None:
        """Tell the adapter which application the following `push`, `pull`, `launch` and `terminate` calls address. Platforms
        whose file transfer is addressed by application (iOS app data containers) need it before `push`; the others ignore it."""

    def bind_temporary_registrar(self, register) -> None:
        """Give the adapter a callback that lists a folder as temporary for cleanup. A build calls it as soon as it creates a
        folder outside its output (not after the build), so a timeout, cancel or crash still removes it."""

    def bind_build_budget(self, seconds: float) -> None:
        """Tell the adapter the stage timeout; a build ends itself inside it instead of being abandoned by the pipeline."""

    def bind_device(self, device: "DeviceInfo") -> None:
        """Tell the adapter which device the pipeline selected and leased, before `build`. A build that signs for a device (iOS)
        must sign for this one, not for the first attached device."""

    @abc.abstractmethod
    def probe(self) -> Capabilities:
        """Report what this host can do for the platform, and why anything is blocked."""

    @abc.abstractmethod
    def discover(self) -> Sequence[DeviceInfo]:
        """List the targets currently reachable. A stale registry entry is never trusted over discovery."""

    @abc.abstractmethod
    def build(self, project: Path, profile: str, out: Path) -> StepOutcome:
        """Produce an installable artifact in `out`."""

    @abc.abstractmethod
    def install(self, device: DeviceInfo, artifact: Path) -> StepOutcome:
        """Install the artifact; an install of a failed build is refused."""

    def prepare(self, device: DeviceInfo, app: str, data_policy: str | None) -> StepOutcome:
        """Called once per run before the plan is pushed: binds the application the run targets and applies the plan's data
        policy (`clean-install` resets the application's data; no other policy may delete anything). Default: nothing to do."""
        return StepOutcome(constants.PASS)

    @abc.abstractmethod
    def push(self, device: DeviceInfo, local: Path, remote: str) -> StepOutcome:
        """Copy a file into the application's data root on the device (for example the plan inbox)."""

    @abc.abstractmethod
    def pull(self, device: DeviceInfo, remote: str, local: Path) -> StepOutcome:
        """Copy a file or folder out of the application's data root."""

    @abc.abstractmethod
    def launch(self, device: DeviceInfo, app: str, args: Sequence[str], log: Path) -> StepOutcome:
        """Start the application with console or log capture into `log`; returns when the process has started."""

    @abc.abstractmethod
    def terminate(self, device: DeviceInfo, app: str) -> StepOutcome:
        """Stop the application and its process tree."""

    @abc.abstractmethod
    def screenshot(self, device: DeviceInfo, out: Path) -> StepOutcome:
        """Capture the device screen to `out`."""

    @abc.abstractmethod
    def health(self, device: DeviceInfo) -> DeviceHealth:
        """Report lock state, thermal state, battery and free space where the platform exposes them."""
