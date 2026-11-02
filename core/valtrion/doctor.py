"""`valtrion doctor`: a read-only probe of what this host can do.

It looks at installed Unity editors and their modules, Xcode and devicectl, adb (including the copy bundled with a Unity Android
module), the macOS GUI session and keychain, free disk, and whether a local GitHub Actions runner is busy. The runner is only
reported: doctor never stops, pauses or reconfigures it. A missing prerequisite is listed under `blocked` with its reason.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Sequence

from . import __version__, buildcache, checkout, compare, core, diskspace, incremental, lease, workroot
from .resolver import HostCapabilities

PROBE_TIMEOUT_SEC = 20
HUB_PATHS_ENV = "GDV_UNITY_HUB_PATHS"

#: Runs a command and returns (returncode, stdout+stderr). Replaced in tests.
Runner = Callable[[Sequence[str]], "tuple[int, str]"]

_MODULE_FOLDERS = {
    "iOSSupport": "ios", "AndroidPlayer": "android", "WindowsStandaloneSupport": "windows", "MacStandaloneSupport": "macos",
    "LinuxStandaloneSupport": "linux", "WebGLSupport": "webgl"}


def run_probe(argv: Sequence[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(
            list(argv), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=PROBE_TIMEOUT_SEC, check=False)
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as error:
        return 127, str(error)
    return done.returncode, (done.stdout + done.stderr).strip()


def hub_roots(os_name: str, environ: dict | None = None) -> list[Path]:
    environment = os.environ if environ is None else environ
    override = environment.get(HUB_PATHS_ENV)
    if override:
        return [Path(item) for item in override.split(os.pathsep) if item]
    if os_name == "windows":
        roots = [Path(environment.get("ProgramFiles", r"C:\Program Files")) / "Unity" / "Hub" / "Editor"]
    elif os_name == "macos":
        roots = [Path("/Applications/Unity/Hub/Editor")]
    else:
        roots = [Path.home() / "Unity" / "Hub" / "Editor"]
    return roots


def find_editors(os_name: str, environ: dict | None = None) -> list[dict]:
    """Editors installed under the Hub paths: `{version, path, modules, adb}`."""
    editors = []
    for root in hub_roots(os_name, environ):
        if not root.is_dir():
            continue
        for folder in sorted(root.iterdir()):
            if not folder.is_dir() or not re.match(r"^\d+\.\d+", folder.name):
                continue
            executable = _editor_executable(folder, os_name)
            if executable is None:
                continue
            playback = [folder / "Editor" / "Data" / "PlaybackEngines", folder / "PlaybackEngines",
                        folder / "Unity.app" / "Contents" / "PlaybackEngines"]
            modules = sorted({name for engines in playback if engines.is_dir() for entry in engines.iterdir()
                              for key, name in _MODULE_FOLDERS.items() if entry.name.lower() == key.lower()})
            if any((engines / "windowsstandalonesupport" / "Variations" / "win64_player_development_il2cpp").is_dir()
                   for engines in playback if engines.is_dir()):
                modules.append("windows-il2cpp")
            adb = None
            for engines in playback:
                candidate = engines / "AndroidPlayer" / "SDK" / "platform-tools" / ("adb.exe" if os_name == "windows" else "adb")
                if candidate.is_file():
                    adb = str(candidate)
            editors.append({"version": folder.name, "path": str(executable), "modules": modules, "adb": adb})
    return editors


def _editor_executable(folder: Path, os_name: str) -> Path | None:
    candidates = {
        "windows": [folder / "Editor" / "Unity.exe"],
        "macos": [folder / "Unity.app" / "Contents" / "MacOS" / "Unity"],
        "linux": [folder / "Editor" / "Unity"]}[os_name]
    return next((path for path in candidates if path.is_file()), None)


def probe_runner(os_name: str, run: Runner) -> dict:
    """State of a local GitHub Actions runner: busy (a Worker is running), idle (only the Listener), or absent."""
    if os_name == "windows":
        code, output = run(["tasklist", "/FO", "CSV", "/NH"])
    else:
        code, output = run(["ps", "-axo", "pid=,comm=,args="])
    if code != 0:
        return {"state": "unknown", "reason": output[:200]}
    listener = len(re.findall(r"Runner\.Listener", output))
    worker = len(re.findall(r"Runner\.Worker", output))
    state = "busy" if worker else "idle" if listener else "absent"
    return {"state": state, "listeners": listener, "workers": worker,
            "note": "reported only; doctor never stops or reconfigures the runner"}


def probe_apple(run: Runner) -> tuple[dict, dict]:
    """Xcode, devicectl, GUI session and keychain on macOS. Returns (facts, blocked)."""
    facts: dict = {}
    blocked: dict = {}
    code, output = run(["xcodebuild", "-version"])
    if code == 0:
        facts["xcode"] = output.splitlines()[0].replace("Xcode", "").strip() if output else "unknown"
    else:
        blocked["xcode"] = f"xcodebuild is not usable: {output[:120]}"
    code, output = run(["xcrun", "devicectl", "--version"])
    if code == 0:
        facts["devicectl"] = output.splitlines()[0] if output else "available"
    else:
        blocked["devicectl"] = f"devicectl is not usable: {output[:120]}"
    uid = os.getuid() if hasattr(os, "getuid") else 0
    code, _ = run(["launchctl", "print", f"gui/{uid}"])
    facts["guiSession"] = code == 0
    if code != 0:
        blocked["guiSession"] = f"no GUI session for uid {uid}; signing steps need a logged-in user (owner action)"
    code, output = run(["security", "find-identity", "-v", "-p", "codesigning"])
    identities = len(re.findall(r"^\s*\d+\)", output, re.MULTILINE)) if code == 0 else 0
    facts["signingIdentities"] = identities
    code, _ = run(["security", "show-keychain-info", str(Path.home() / "Library" / "Keychains" / "login.keychain-db")])
    facts["keychainReadable"] = code == 0
    if identities == 0:
        blocked["iosSigning"] = "no valid code signing identity is visible"
    elif not facts["guiSession"]:
        blocked["iosSigning"] = "signing needs the GUI session (keychain)"
    return facts, blocked


#: Where tools usually live that a non-interactive SSH session does not have on PATH (its PATH is the bare system default, without the
#: package manager's folder). Searched after PATH, so a tool on PATH always wins.
EXTRA_TOOL_DIRS = {"macos": ("/opt/homebrew/bin", "/usr/local/bin"), "linux": ("/usr/local/bin", "/usr/local/sbin", "/snap/bin")}


def which_tool(name: str, os_name: str, extra_dirs: Sequence[str] | None = None) -> str | None:
    """`shutil.which` on PATH, then in the usual install folders: a host reached over SSH has no login PATH, and a tool that is only
    absent from that PATH is not missing from the host."""
    found = shutil.which(name)
    if found:
        return found
    for folder in EXTRA_TOOL_DIRS.get(os_name, ()) if extra_dirs is None else extra_dirs:
        found = shutil.which(name, path=folder)
        if found:
            return found
    return None


def probe_adb(editors: Sequence[dict], os_name: str, run: Runner, extra_dirs: Sequence[str] | None = None) -> tuple[dict, dict]:
    candidates = [("path", which_tool("adb", os_name, extra_dirs))] + [(f"unity {e['version']}", e["adb"]) for e in editors if e.get("adb")]
    for source, path in candidates:
        if not path:
            continue
        code, output = run([path, "version"])
        if code == 0:
            return {"adb": path, "adbSource": source, "adbVersion": output.splitlines()[0] if output else ""}, {}
    return {}, {"adb": "adb is not on PATH and no Unity Android module bundles it"}


def disk_report(path: Path) -> dict:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    return {"path": str(path), "freeGb": round(usage.free / 1e9, 1), "totalGb": round(usage.total / 1e9, 1)}


def protected_path_warnings(os_name: str, locations: dict, home: Path | None = None, environ: dict | None = None,
                            packaged: "bool | None" = None, virtualized: dict | None = None) -> list[str]:
    """One warning per valtrion location that lies in a folder the operating system protects (a background job or build tool that
    touches it triggers a permission prompt on macOS or a block on Windows, and nothing can answer one unattended) or, on Windows,
    in a packaged app's virtualized AppData (`workroot.virtualized_location`). `virtualized` maps a location name to a reason
    already computed for it, so the caller and this function agree on one answer."""
    warnings = []
    for name, path in locations.items():
        if virtualized is not None and name in virtualized:
            virtual = virtualized[name]
        else:
            virtual = workroot.virtualized_location(Path(path), os_name, environ, home, packaged)
        reason = workroot.protected_location(Path(path), os_name, home) or virtual
        if reason:
            override = ("; GDV_WORK_ROOT overrides the work root for one process, and either should name a short absolute path"
                        if name == "work root" else "")
            warnings.append(f"{name} {path} is {reason}; move it with host.json (`workRoot`, `artifactsRoot`) or VALTRION_HOME{override}")
    return warnings


def work_root_length_warnings(os_name: str, work_root: Path, long_paths_enabled: "bool | None",
                              virtualized: bool = False, android: bool = False) -> list[str]:
    """A warning when the work root is too long for a Unity project checkout on Windows with long paths off (see
    `workroot.path_length_warning`). None when the root is virtualized: that warning already says to move it to a short absolute path."""
    reason = None if virtualized else workroot.path_length_warning(work_root, os_name, long_paths_enabled, android=android)
    return [f"work root {work_root} {reason}"] if reason else []


def work_root_report(work_root: Path) -> dict:
    roots = workroot.WorkRoots(work_root)
    report = {"path": str(work_root), "usage": workroot.usage(roots), "disk": disk_report(work_root)}
    report["staleJobDirectories"] = [p.name for p in workroot.stale_job_directories(roots)]
    return report


def storage_report(work_root: Path, artifacts_root: Path, min_free_gib: float | None = None, home: Path | None = None) -> dict:
    """Disk facts a build host needs: free space where builds are made, what the three big consumers hold (job folders of the work
    root, run folders, published builds), and the `disk.minFreeGiB` threshold with whether the host is below it."""
    cache = buildcache.builds_root(home)
    free = diskspace.free_bytes(work_root)
    report = {
        "workRoot": {"path": str(work_root), "bytes": diskspace.folder_bytes(work_root) if work_root.is_dir() else 0},
        "artifactsRoot": {"path": str(artifacts_root), "bytes": diskspace.folder_bytes(artifacts_root) if artifacts_root.is_dir() else 0},
        "buildCache": {"path": str(cache), "bytes": diskspace.folder_bytes(cache) if cache.is_dir() else 0,
                       "entries": len(buildcache.entries(cache))},
        "incremental": [{"path": str(path), "bytes": diskspace.folder_bytes(path)} for path in incremental.folders(home)],
        "freeBytes": free}
    if min_free_gib:
        report["minFreeGiB"] = min_free_gib
        report["belowMinimum"] = free < min_free_gib * diskspace.GIB
    return report


def checkouts_report(projects: Sequence, home: Path | None = None, sizes: bool = False) -> dict:
    """The managed build checkouts of `host.json` `projects` (their commit, size and who holds each one's lease), and the project folders
    outside them that hold a `unity-project:` lease right now."""
    held: dict[str, dict] = {}
    try:
        for status in lease.LeaseManager(home or core.valtrion_home()).list_all():
            if status.state == "held" and status.resource.startswith("unity-project:") and status.record is not None:
                held[status.resource] = {"owner": status.record.owner, "session": status.record.session, "purpose": status.record.purpose}
    except OSError:
        held = {}
    managed_resources: set[str] = set()
    listed = []
    for project in projects:
        for item in project.checkouts:
            folder = project.project_dir(item)
            resource = lease.unity_project_resource(folder)
            managed_resources.add(resource)
            exists = item.path.is_dir() and (item.path / ".git").exists()
            head = None
            if exists:
                code, output = compare.run_git(item.path, ["rev-parse", "HEAD"])
                head = output.strip() if code == 0 else None
            entry = {"project": project.name, "id": item.id, "path": str(item.path), "exists": exists, "commit": head,
                     "bytes": diskspace.folder_bytes(item.path) if exists and sizes else None, "lease": held.get(resource),
                     "marked": exists and (item.path / checkout.MARKER).is_file()}
            record = None if exists else checkout.recorded(home or core.valtrion_home(), item.path)
            if record is not None:  # a job used it before: it was created and is gone now, not "not created yet"
                entry["missing"] = True
                entry["lastUsed"] = record.get("lastUsed")
            listed.append(entry)
    return {"managed": listed, "unmanagedLeases": [{"resource": resource, **info} for resource, info in sorted(held.items())
                                                    if resource not in managed_resources]}


def build_platforms(os_name: str, editors: Sequence[dict], apple_ok: bool) -> list[str]:
    if not editors:
        return []
    modules = {m for editor in editors for m in editor["modules"]}
    platforms = {"editor"}
    if os_name in ("windows", "macos", "linux"):
        platforms.add(os_name)
    if "android" in modules:
        platforms.add("android")
    if "ios" in modules and os_name == "macos" and apple_ok:
        platforms.add("ios")
    return sorted(platforms)


def probe_local(
        os_name: str | None = None, run: Runner = run_probe, artifacts_root: Path | None = None,
        environ: dict | None = None, work_root: Path | None = None, min_free_gib: float | None = None,
        projects: Sequence = (), checkout_sizes: bool = False) -> dict:
    """The full report of this host. Pure of side effects except running read-only commands."""
    os_name = os_name or core.local_os()
    work_root = work_root or workroot.default_work_root(os_name)
    editors = find_editors(os_name, environ)
    blocked: dict = {}
    tools: dict = {}
    if not editors:
        blocked["unity"] = "no Unity editor was found under the Hub paths"
    apple: dict = {}
    if os_name == "macos":
        apple, apple_blocked = probe_apple(run)
        blocked.update(apple_blocked)
        tools.update({key: value for key, value in apple.items() if key in ("xcode", "devicectl")})
    else:
        for name in ("xcode", "devicectl", "guiSession", "iosSigning"):
            blocked[name] = f"not available on {os_name}"
    adb_facts, adb_blocked = probe_adb(editors, os_name, run)
    blocked.update(adb_blocked)
    tools.update({key: value for key, value in adb_facts.items() if key in ("adb", "adbVersion")})
    xcode_ok = "xcode" in apple
    virtual_work_root = workroot.virtualized_location(work_root, os_name)  # computed once: both warnings below depend on it
    signing = os_name == "macos" and "iosSigning" not in blocked and xcode_ok and apple.get("guiSession", False)
    capabilities: dict = {
        "unityEditors": [{"version": e["version"], "modules": e["modules"]} for e in editors],
        "buildPlatforms": build_platforms(os_name, editors, xcode_ok),
        "iosSigning": bool(signing), "adb": bool(adb_facts)}
    if xcode_ok:
        capabilities["xcode"] = apple["xcode"]
    checkouts = checkouts_report(projects, sizes=checkout_sizes)
    lost = [f"managed checkout {item['project']}/{item['id']} at {item['path']} was created and is missing now (last used "
            f"{item.get('lastUsed') or 'unknown'}): the next build reclones it and Unity imports Library again; managed checkouts are "
            "working state, not cache" for item in checkouts["managed"] if item.get("missing")]
    return {
        "host": {"id": core.local_host_id(), "os": os_name}, "core": {"version": __version__}, "capabilities": capabilities, "tools": tools,
        "editors": [{"version": e["version"], "path": e["path"]} for e in editors], "gui": {
            key: apple[key] for key in ("guiSession", "keychainReadable", "signingIdentities") if key in apple},
        "disk": disk_report(artifacts_root or core.valtrion_home()), "githubRunner": probe_runner(os_name, run), "blocked": blocked,
        "workRoot": work_root_report(work_root),
        "storage": storage_report(work_root, artifacts_root or core.valtrion_home() / "artifacts", min_free_gib),
        "checkouts": checkouts,
        "warnings": lost + protected_path_warnings(os_name, {
            "work root": work_root, "artifacts root": artifacts_root or core.valtrion_home() / "artifacts", "valtrion home": core.valtrion_home()},
            virtualized={"work root": virtual_work_root})
        + work_root_length_warnings(os_name, work_root, workroot.windows_long_paths_enabled() if os_name == "windows" else None,
                                    bool(virtual_work_root), "android" in capabilities["buildPlatforms"])}


def to_host_capabilities(report: dict, devices: Sequence = ()) -> HostCapabilities:
    caps = report["capabilities"]
    return HostCapabilities(
        unity_editors={item["version"]: tuple(item["modules"]) for item in caps.get("unityEditors", [])},
        build_platforms=tuple(caps.get("buildPlatforms", ())), xcode="xcode" in caps, ios_signing=bool(caps.get("iosSigning")),
        adb=bool(caps.get("adb")), devices=tuple(devices))


def render(report: dict) -> str:
    caps = report["capabilities"]
    lines = [f"host {report['host']['id']} ({report['host']['os']}), valtrion {report.get('core', {}).get('version', 'unknown')}"]
    editors = ", ".join(f"{e['version']} [{', '.join(e['modules']) or 'no modules'}]" for e in caps["unityEditors"]) or "none"
    lines.append(f"  unity editors: {editors}")
    lines.append(f"  can build: {', '.join(caps['buildPlatforms']) or 'nothing'}")
    lines.append(f"  xcode: {caps.get('xcode', 'no')}  ios signing: {'yes' if caps['iosSigning'] else 'no'}  adb: {'yes' if caps['adb'] else 'no'}")
    disk = report["disk"]
    lines.append(f"  disk: {disk['freeGb']} GB free of {disk['totalGb']} GB at {disk['path']}")
    runner = report["githubRunner"]
    lines.append(f"  github runner: {runner['state']}")
    work = report.get("workRoot")  # absent in a report cached from a host that runs an older valtrion
    if work:
        lines.append(f"  work root: {work['path']} ({work['usage']['jobDirectories']} job dir(s), {work['disk']['freeGb']} GB free)")
    for name, reason in sorted(report["blocked"].items()):
        lines.append(f"  BLOCKED {name}: {reason}")
    for warning in report.get("warnings", []):
        lines.append(f"  WARNING {warning}")
    storage = report.get("storage")  # absent in a report cached from a host that runs an older valtrion
    if storage:
        gib = 1024 ** 3
        lines.append(f"  storage: work root {storage['workRoot']['bytes'] / gib:.1f} GiB, run folders {storage['artifactsRoot']['bytes'] / gib:.1f} GiB, "
                     f"build cache {storage['buildCache']['bytes'] / gib:.1f} GiB ({storage['buildCache']['entries']} build(s)), "
                     f"{storage['freeBytes'] / gib:.1f} GiB free")
        for item in storage.get("incremental", []):
            lines.append(f"  incremental build folder {item['path']}: {item['bytes'] / gib:.1f} GiB")
        for item in (report.get("checkouts") or {}).get("managed", []):
            state = (f"at {item['commit'][:12]}" if item["commit"] else "not a git checkout" if item["exists"] else
                     f"was created, now missing (last used {item.get('lastUsed') or 'unknown'})" if item.get("missing") else "not created yet")
            holder = f", leased by {item['lease']['owner']} ({item['lease']['purpose']})" if item.get("lease") else ""
            lines.append(f"  checkout {item['project']}/{item['id']}: {item['path']} {state}, {(item['bytes'] or 0) / gib:.1f} GiB{holder}")
        for item in (report.get("checkouts") or {}).get("unmanagedLeases", []):
            lines.append(f"  project folder outside host.json projects holds a lease: {item['resource']} ({item['owner']}, {item['purpose']})")
        if storage.get("belowMinimum"):
            lines.append(f"  WARNING free space is below disk.minFreeGiB ({storage['minFreeGiB']:g} GiB): the next build cleans up first and is BLOCKED if that is not enough")
    if work and work["staleJobDirectories"]:
        lines.append(f"  WARNING {len(work['staleJobDirectories'])} abandoned job dir(s) in the work root; `valtrion workroot sweep` removes them")
    return "\n".join(lines)
