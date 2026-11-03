"""Leases on exclusive resources, shared by every session on one host.

A lease is a lock file created with O_CREAT|O_EXCL under `<valtrion home>/leases/`. It holds a `gdv.lease/1` document. The process that
supervises the work renews it. A lease is stale only when its holder process is dead AND its TTL has expired; a live lease is never
taken over. A run that finds the resource leased waits a bounded time or reports BLOCKED naming the holder. A counted resource
(for example concurrent Editors) is N leases `<name>:slot1..N` under the same rules. Leases are always taken on the host that owns
the resource; a remote caller runs `valtrion lease` over the transport.
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import constants, core

SCHEMA = "gdv.lease/1"
#: A lock file that cannot be read (being written, or torn by a crash) is treated as held until it is this old.
UNREADABLE_GRACE_SEC = 10.0
_REPLACE_GUARD_MAX_AGE_SEC = 30.0
#: Waiting lines live in `<leases>/queue/<resource>/`, one `.ticket` file per waiter (kept out of `list_all`, which reads `*.lock`).
QUEUE_FOLDER = "queue"


@dataclass(frozen=True)
class LeaseRecord:
    resource: str
    owner: str
    session: str
    pid: int
    host: str
    acquired_utc: str
    renewed_utc: str
    ttl_sec: int
    purpose: str
    worktree: str = ""

    def to_document(self) -> dict:
        document = {
            "schema": SCHEMA, "resource": self.resource, "owner": self.owner, "session": self.session, "pid": self.pid,
            "host": self.host, "acquiredUtc": self.acquired_utc, "renewedUtc": self.renewed_utc, "ttlSec": self.ttl_sec,
            "purpose": self.purpose,
        }
        if self.worktree:
            document["worktree"] = self.worktree
        return document

    @staticmethod
    def from_document(document: dict) -> "LeaseRecord":
        return LeaseRecord(
            resource=document["resource"], owner=document["owner"], session=document["session"], pid=int(document["pid"]),
            host=document["host"], acquired_utc=document["acquiredUtc"], renewed_utc=document["renewedUtc"],
            ttl_sec=int(document["ttlSec"]), purpose=document.get("purpose", ""), worktree=document.get("worktree", ""))

    def same_holder(self, owner: str, session: str, pid: int) -> bool:
        return self.owner == owner and self.session == session and self.pid == pid

    def describe(self) -> str:
        return (f"leased by {self.owner} (session {self.session}, pid {self.pid}, host {self.host}) since {self.acquired_utc}, "
                f"renewed {self.renewed_utc}, ttl {self.ttl_sec}s: {self.purpose or 'no purpose given'}")


@dataclass(frozen=True)
class AcquireResult:
    acquired: bool
    lease: LeaseRecord | None = None
    holder: LeaseRecord | None = None
    reclaimed: LeaseRecord | None = None  # a stale lease this acquisition replaced
    reason: str | None = None

    @property
    def status(self) -> str:
        return constants.PASS if self.acquired else constants.BLOCKED


@dataclass(frozen=True)
class LeaseStatus:
    resource: str
    state: str  # free, held or stale
    record: LeaseRecord | None = None
    unreadable: bool = False


_RESOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def lease_file_name(resource: str) -> str:
    """File name of a resource's lock. The name must be a contract identifier, so it cannot hold a path separator or `%`; a `:`
    (device:<id>), which Windows forbids in file names, becomes `%3A` and cannot collide with a literal one."""
    if not _RESOURCE.fullmatch(resource) or len(resource) > 200:
        raise ValueError(f"invalid resource name {resource!r}: use letters, digits and ._:- (the identifier pattern of the contracts)")
    return resource.replace(":", "%3A") + ".lock"


def slot_resource(name: str, slot: int) -> str:
    return f"{name}:slot{slot}"


def leases_directory(home: Path | None = None) -> Path:
    """`<home>/leases`, or the absolute `leasesDir` of host.json. The setting is temporary: during the move from the earlier tool it lets
    this CLI and the old one share one lease directory, so they never both hold the same device (the file format is the same)."""
    base = home or core.valtrion_home()
    try:
        document = core.read_json(base / "host.json")
    except FileNotFoundError:
        return base / "leases"
    except (OSError, ValueError) as error:  # a broken host.json must not silently split the lease directory from the other tool's
        raise ValueError(f"cannot read {base / 'host.json'}: {error}") from error
    configured = document.get("leasesDir") if isinstance(document, dict) else None
    if configured is None:
        return base / "leases"
    if not isinstance(configured, str) or not Path(configured).expanduser().is_absolute():
        raise ValueError("host.json: leasesDir must be an absolute path (or start with ~)")
    return Path(configured).expanduser()


#: The counted resource every Unity process on a host takes one slot of: an Editor run, an export, a native build.
UNITY_EDITOR_RESOURCE = "unity-editor"
#: Concurrent Unity processes a host allows, unless `host.json` says otherwise: 2 on every platform.
DEFAULT_UNITY_EDITOR_SLOTS = 2


def unity_editor_capacity(home: Path | None = None) -> int:
    """The total number of `unity-editor` slots of this host. It is a property of the host, never of a provider: every claimant
    (the pipeline's build stage, the Editor provider) must pass this same total to `acquire_slot`, otherwise one that counts 1 and
    one that counts 2 both believe they hold the only free slot. `unityEditorSlots` (a positive integer) in `host.json` overrides
    the default (2 on every platform)."""
    base = home or core.valtrion_home()
    default = DEFAULT_UNITY_EDITOR_SLOTS
    try:
        document = core.read_json(base / "host.json")
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read {base / 'host.json'}: {error}") from error
    configured = document.get("unityEditorSlots") if isinstance(document, dict) else None
    if configured is None:
        return default
    if isinstance(configured, bool) or not isinstance(configured, int) or configured < 1:
        raise ValueError("host.json: unityEditorSlots must be a positive integer")
    return configured


def unity_project_resource(project: Path) -> str:
    """Lease name of one Unity project folder. Unity opens a project in one process at a time (two Editors, or an Editor and an export,
    on one folder corrupt each other's Library), so a run that opens the project holds this lease besides a `unity-editor` slot. The name
    is made from the resolved path (symlinks followed) and, on Windows and macOS, lower-cased."""
    resolved = os.path.normcase(os.path.realpath(project))
    if core.local_os() in ("windows", "macos"):  # their default file systems ignore case: `Proj` and `proj` are one folder
        resolved = resolved.lower()
    return "unity-project:" + core.sha256_bytes(resolved.encode("utf-8"))[:16]


#: Called while an acquire waits: (why it is blocked, seconds waited so far).
WaitNotice = Callable[[str, float], None]
#: Shortest time between two calls of an `on_wait` callback.
WAIT_NOTICE_SEC = 30.0


def waiting_message(reason: str, waited_sec: float) -> str:
    """The line shown for an `on_wait` call (the message of the job's `log` event, the stderr line of `lease acquire --wait`)."""
    return f"lease waiting {waited_sec:.0f} s: {reason}"


class _WaitNotifier:
    """Calls the callback after the first failed attempt and then at most every `WAIT_NOTICE_SEC` (of the injected monotonic clock)."""

    def __init__(self, callback: WaitNotice | None, started: float) -> None:
        self._callback = callback
        self._started = started
        self._last: float | None = None

    def tick(self, reason: str, now: float) -> None:
        if self._callback is None or (self._last is not None and now - self._last < WAIT_NOTICE_SEC):
            return
        self._last = now
        try:
            self._callback(reason, max(0.0, now - self._started))
        except Exception:  # noqa: BLE001 - a notice (closed stderr, a failed event write) must never abort the acquisition
            pass


class LeaseManager:
    """All lease operations of one host. Time, process liveness and sleeping are injected so tests control them."""

    def __init__(
            self, home: Path | None = None, host_id: str | None = None, clock: Callable[[], datetime] = core.now,
            is_alive: Callable[[int], bool] = core.process_alive, sleep: Callable[[float], None] = time.sleep,
            monotonic: Callable[[], float] = time.monotonic) -> None:
        self.directory = leases_directory(home)
        self.host_id = host_id or core.local_host_id()
        self._clock = clock
        self._is_alive = is_alive
        self._sleep = sleep
        self._monotonic = monotonic

    def path_for(self, resource: str) -> Path:
        return self.directory / lease_file_name(resource)

    # -- reading -------------------------------------------------------------------------------------------------------

    def _read(self, path: Path) -> tuple[LeaseRecord | None, bool]:
        """Return (record, unreadable). A missing file is (None, False); a file that cannot be parsed is (None, True)."""
        try:
            document = core.read_json(path, attempts=3)
        except FileNotFoundError:
            return None, False
        except (OSError, ValueError):
            return None, True
        try:
            if not isinstance(document, dict) or document.get("schema") != SCHEMA:
                return None, True
            return LeaseRecord.from_document(document), False
        except (KeyError, TypeError, ValueError):
            return None, True

    def _expired(self, record: LeaseRecord) -> bool:
        try:
            renewed = core.parse_utc(record.renewed_utc)
        except ValueError:
            return True
        return self._clock() - renewed > timedelta(seconds=record.ttl_sec)

    def is_stale(self, record: LeaseRecord) -> bool:
        """Stale only when the holder is dead AND the TTL has expired; one condition alone keeps the lease."""
        return self._expired(record) and not self._is_alive(record.pid)

    def _unreadable_is_stale(self, path: Path) -> bool:
        try:
            age = self._clock().timestamp() - path.stat().st_mtime
        except OSError:
            return False
        return age > UNREADABLE_GRACE_SEC

    def status(self, resource: str) -> LeaseStatus:
        path = self.path_for(resource)
        record, unreadable = self._read(path)
        if record is None and not unreadable:
            return LeaseStatus(resource, "free")
        if record is None:
            return LeaseStatus(resource, "stale" if self._unreadable_is_stale(path) else "held", None, True)
        return LeaseStatus(resource, "stale" if self.is_stale(record) else "held", record)

    def list_all(self) -> list[LeaseStatus]:
        if not self.directory.is_dir():
            return []
        result = []
        for path in sorted(self.directory.glob("*.lock")):
            resource = path.name[: -len(".lock")].replace("%3A", ":")
            result.append(self.status(resource))
        return result

    # -- acquiring -----------------------------------------------------------------------------------------------------

    def _new_record(self, resource: str, owner: str, session: str, pid: int, ttl_sec: int, purpose: str, worktree: str) -> LeaseRecord:
        stamp = core.format_utc(self._clock())
        return LeaseRecord(resource, owner, session, pid, self.host_id, stamp, stamp, ttl_sec, purpose, worktree)

    def _create(self, path: Path, record: LeaseRecord) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(core.to_json(record.to_document()).encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        return True

    def _reclaim(self, path: Path, observed: LeaseRecord | None) -> bool:
        """Remove a stale lock under a short-lived guard so two reclaimers never delete a lease the other just created."""
        guard = path.with_name(path.name + ".reclaim")
        try:
            descriptor = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                if self._clock().timestamp() - guard.stat().st_mtime > _REPLACE_GUARD_MAX_AGE_SEC:
                    guard.unlink()  # the previous reclaimer died holding the guard
            except OSError:
                pass
            return False
        os.close(descriptor)
        try:
            current, unreadable = self._read(path)
            still_stale = (current is not None and self.is_stale(current) and current == observed) or (
                current is None and unreadable and observed is None and self._unreadable_is_stale(path))
            if not still_stale:
                return False
            for _ in range(10):
                try:
                    path.unlink()
                    return True
                except FileNotFoundError:
                    return True
                except PermissionError:
                    self._sleep(0.05)
            return False
        finally:
            try:
                guard.unlink()
            except OSError:
                pass

    def try_acquire(self, resource: str, owner: str, session: str, ttl_sec: int, purpose: str = "", pid: int | None = None,
                    worktree: str = "") -> AcquireResult:
        """One attempt. Idempotent for the same owner, session and pid: a repeated call (a retried remote call) renews it."""
        if ttl_sec < 1:
            raise ValueError("ttl must be positive")
        holder_pid = os.getpid() if pid is None else pid
        path = self.path_for(resource)
        record = self._new_record(resource, owner, session, holder_pid, ttl_sec, purpose, worktree)
        reclaimed: LeaseRecord | None = None
        for _ in range(3):
            if self._create(path, record):
                return AcquireResult(True, record, reclaimed=reclaimed)
            existing, unreadable = self._read(path)
            if existing is None and not unreadable:
                continue  # released between the attempt and the read
            if existing is not None and existing.same_holder(owner, session, holder_pid):
                if self.renew(resource, session, holder_pid, owner):
                    refreshed, _ = self._read(path)
                    return AcquireResult(True, refreshed or existing, reclaimed=reclaimed)
                continue
            stale = self.is_stale(existing) if existing is not None else self._unreadable_is_stale(path)
            if not stale:
                break
            if self._reclaim(path, existing):
                reclaimed = existing or reclaimed
                continue
            break
        holder, _ = self._read(path)
        reason = f"{resource} is {holder.describe()}" if holder is not None else f"{resource} is held (its lock file is being written)"
        return AcquireResult(False, None, holder, reclaimed, reason)

    def acquire(self, resource: str, owner: str, session: str, ttl_sec: int, purpose: str = "", pid: int | None = None,
                worktree: str = "", wait_sec: float = 0.0, poll_sec: float = 1.0, on_wait: WaitNotice | None = None) -> AcquireResult:
        """Take the lease, waiting at most `wait_sec`. Never takes a live lease; returns BLOCKED naming the holder instead.
        `on_wait(reason, waited_seconds)` is called while waiting: after the first failed attempt, then at most every
        `WAIT_NOTICE_SEC` seconds."""
        holder_pid = os.getpid() if pid is None else pid

        def holds() -> bool:
            record, _ = self._read(self.path_for(resource))
            return record is not None and record.same_holder(owner, session, holder_pid)

        return self._queued(resource, lambda: self.try_acquire(resource, owner, session, ttl_sec, purpose, pid, worktree), holds,
                            owner, session, holder_pid, ttl_sec, wait_sec, poll_sec, on_wait)

    def acquire_slot(self, name: str, slots: int, owner: str, session: str, ttl_sec: int, purpose: str = "", pid: int | None = None,
                     worktree: str = "", wait_sec: float = 0.0, poll_sec: float = 1.0, on_wait: WaitNotice | None = None) -> AcquireResult:
        """Take one of `slots` leases of a counted resource; a holder that already has a slot gets that slot again. `on_wait` is
        called as in `acquire`."""
        if slots < 1:
            raise ValueError("a counted resource needs at least one slot")
        holder_pid = os.getpid() if pid is None else pid

        def owned_slot() -> int | None:
            # A holder that already owns a slot keeps that one, even when a lower slot has been freed since.
            for slot in range(1, slots + 1):
                record, _ = self._read(self.path_for(slot_resource(name, slot)))
                if record is not None and record.same_holder(owner, session, holder_pid):
                    return slot
            return None

        def attempt() -> AcquireResult:
            slot = owned_slot()
            if slot is not None:
                return self.try_acquire(slot_resource(name, slot), owner, session, ttl_sec, purpose, holder_pid, worktree)
            reasons = []
            for slot in range(1, slots + 1):
                result = self.try_acquire(slot_resource(name, slot), owner, session, ttl_sec, purpose, holder_pid, worktree)
                if result.acquired:
                    return result
                reasons.append(result.reason or f"{slot_resource(name, slot)} is held")
            return AcquireResult(False, None, None, None, f"all {slots} slots of {name} are leased: " + "; ".join(reasons))

        return self._queued(name, attempt, lambda: owned_slot() is not None, owner, session, holder_pid, ttl_sec, wait_sec, poll_sec, on_wait)

    # -- the waiting line ----------------------------------------------------------------------------------------------

    def _queue_directory(self, name: str) -> Path:
        return self.directory / QUEUE_FOLDER / lease_file_name(name)[: -len(".lock")]

    def _enqueue(self, name: str, owner: str, session: str, pid: int, ttl_sec: int, poll_sec: float = 1.0) -> Path:
        """Join the waiting line of a resource. Tickets sort by the time they were made (never before the newest ticket already in the
        line, so a clock that stands still or steps back cannot put a newcomer ahead), so the longest waiter is first."""
        directory = self._queue_directory(name)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = int(self._clock().timestamp() * 1_000_000)
        newest = max((int(p.name.split("-", 1)[0]) for p in directory.glob("*.ticket") if p.name.split("-", 1)[0].isdigit()), default=-1)
        ticket = directory / f"{max(stamp, newest + 1):020d}-{os.getpid()}-{uuid.uuid4().hex[:8]}.ticket"
        self._write_ticket(ticket, owner, session, pid, ttl_sec, core.format_utc(self._clock()), poll_sec)
        return ticket

    def _write_ticket(self, ticket: Path, owner: str, session: str, pid: int, ttl_sec: int, enqueued: str, poll_sec: float) -> None:
        core.atomic_write_json(ticket, {"owner": owner, "session": session, "pid": pid, "ttlSec": ttl_sec, "pollSec": poll_sec,
                                        "enqueuedUtc": enqueued, "renewedUtc": core.format_utc(self._clock())})

    def _renew_ticket(self, ticket: Path) -> None:
        try:
            document = core.read_json(ticket, attempts=2)
            self._write_ticket(ticket, document["owner"], document["session"], int(document["pid"]), int(document["ttlSec"]),
                               document["enqueuedUtc"], float(document.get("pollSec", 1.0)))
        except (OSError, ValueError, KeyError, TypeError):
            pass  # a waiter whose ticket is gone is simply not in line any more; it joins again on the next failed attempt

    def _ticket_is_stale(self, ticket: Path) -> bool:
        """Same rule as a lease: the waiter's process is dead AND its renewal is older than the TTL (an unreadable ticket: old)."""
        try:
            document = core.read_json(ticket, attempts=2)
            idle = (self._clock() - core.parse_utc(document["renewedUtc"])).total_seconds()
            poll = max(0.0, float(document.get("pollSec", 1.0)))
            # A waiter renews at every poll, so the line moves on soon after one stops: a few polls when its process is dead, a
            # longer silence (a reused or shared pid, a stopped process) when it looks alive. The lease TTL does not matter here.
            return (idle > max(5.0, 5 * poll) and not self._is_alive(int(document["pid"]))) or idle > max(30.0, 10 * poll)
        except FileNotFoundError:
            return False
        except (OSError, ValueError, KeyError, TypeError):
            return self._unreadable_is_stale(ticket)

    def _ahead(self, name: str, mine: Path | None) -> str | None:
        """Who is in line before `mine` (or, without a ticket, before a newcomer): a description, or None when the caller is first.
        Tickets of dead waiters are removed on the way."""
        directory = self._queue_directory(name)
        try:
            tickets = sorted(directory.glob("*.ticket"))
        except OSError:
            return None
        for ticket in tickets:
            if mine is not None and ticket == mine:
                return None
            if self._ticket_is_stale(ticket):
                self._remove_ticket(ticket)
                continue
            if not ticket.exists():
                continue
            try:
                waiter = core.read_json(ticket, attempts=2)
                who = f"{waiter['owner']} (session {waiter['session']}, queued {waiter['enqueuedUtc']})"
            except (OSError, ValueError, KeyError, TypeError):
                who = "another waiter"
            return who
        return None

    def _queued(self, name: str, attempt: Callable[[], AcquireResult], holds: Callable[[], bool], owner: str, session: str, pid: int,
                ttl_sec: int, wait_sec: float, poll_sec: float, on_wait: WaitNotice | None) -> AcquireResult:
        """Run `attempt` until it succeeds or `wait_sec` ends, but only when the caller is first in the resource's waiting line, so the
        longest waiter gets the resource and a later one cannot overtake it. A caller that does not wait (`wait_sec` 0) gets no
        ticket but still does not overtake live waiters. A caller that already holds the resource is not delayed."""
        started = self._monotonic()
        deadline = started + max(0.0, wait_sec)
        notice = _WaitNotifier(on_wait, started)
        ticket: Path | None = None
        try:
            while True:
                ahead = None if holds() else self._ahead(name, ticket)
                result = attempt() if ahead is None else AcquireResult(False, None, None, None,
                                                                       f"{name} is next for {ahead}, who has waited longer")
                if result.acquired or self._monotonic() >= deadline:
                    return result
                if ticket is None or not ticket.exists():
                    ticket = self._enqueue(name, owner, session, pid, ttl_sec, poll_sec)
                else:
                    self._renew_ticket(ticket)
                notice.tick(result.reason or f"{name} is held", self._monotonic())
                self._sleep(min(poll_sec, max(0.0, deadline - self._monotonic())))
        finally:
            if ticket is not None:
                self._remove_ticket(ticket)

    def _remove_ticket(self, ticket: Path) -> None:
        """Delete a ticket; a reader that has it open (Windows) makes the delete fail for a moment, so it is retried."""
        for _ in range(20):
            try:
                ticket.unlink()
                return
            except FileNotFoundError:
                return
            except OSError:
                time.sleep(0.02)  # real time: a reader holds the file only for the instant it takes to read it

    # -- renewing and releasing ----------------------------------------------------------------------------------------

    def renew(self, resource: str, session: str, pid: int, owner: str | None = None) -> bool:
        """Refresh `renewedUtc`. Fails (False) when the lease is gone or belongs to another holder."""
        path = self.path_for(resource)
        record, _ = self._read(path)
        if record is None or record.session != session or record.pid != pid or (owner is not None and record.owner != owner):
            return False
        refreshed = LeaseRecord(
            record.resource, record.owner, record.session, record.pid, record.host, record.acquired_utc,
            core.format_utc(self._clock()), record.ttl_sec, record.purpose, record.worktree)
        core.atomic_write_json(path, refreshed.to_document())
        return True

    def release(self, resource: str, session: str, pid: int) -> bool:
        """Remove the lease if this holder owns it. Releasing a lease that is already gone is not an error (idempotent)."""
        path = self.path_for(resource)
        record, unreadable = self._read(path)
        if record is None:
            return not unreadable
        if record.session != session or record.pid != pid:
            return False
        for _ in range(10):
            try:
                path.unlink()
                return True
            except FileNotFoundError:
                return True
            except PermissionError:
                self._sleep(0.05)
        return False


class LeaseKeeper:
    """Renews a set of leases from a background thread while the supervised work runs."""

    def __init__(self, manager: LeaseManager, session: str, pid: int, interval_sec: float, owner: str | None = None) -> None:
        self._manager = manager
        self._session = session
        self._pid = pid
        self._owner = owner
        self._interval = interval_sec
        self._resources: list[str] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost: list[str] = []

    def add(self, resource: str) -> None:
        with self._lock:
            if resource not in self._resources:
                self._resources.append(resource)

    def release_one(self, resource: str) -> bool | None:
        """Stop renewing one lease and release it now, while the others stay held. Returns whether the release succeeded, or None
        when the keeper does not hold it (already released, or never taken)."""
        with self._lock:
            if resource not in self._resources:
                return None
            self._resources.remove(resource)  # `lost` is kept: a lease lost while it was held is still reported at the end
            return self._manager.release(resource, self._session, self._pid)

    def replace(self, lost: str, taken: str) -> None:
        """A lost lease was taken again (possibly as another slot of the same counted resource): keep renewing the new one."""
        with self._lock:
            self._resources = [r for r in self._resources if r != lost]
            if taken not in self._resources:
                self._resources.append(taken)
            self.lost = [r for r in self.lost if r != lost]

    def held(self) -> list[str]:
        with self._lock:
            return list(self._resources)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="valtrion-lease-keeper", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            with self._lock:
                # Keep publication inside the ownership lock: release/replace must wait for an in-flight renewal.
                for resource in self._resources:
                    try:
                        renewed = self._manager.renew(resource, self._session, self._pid, self._owner)
                    except Exception:  # noqa: BLE001 - a failed renewal must be recorded, never end the thread silently
                        renewed = False
                    if not renewed and resource not in self.lost:
                        self.lost.append(resource)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def release_all(self) -> list[str]:
        """Release every lease still held; returns the resources that could not be released."""
        with self._lock:
            resources = list(self._resources)
            self._resources.clear()
            return [resource for resource in resources if not self._manager.release(resource, self._session, self._pid)]
