"""A fake `adb` for the Android adapter tests: a small device simulated in a folder named by FAKE_ADB_STATE.

`state.json` describes the attached devices; `sdcard/` and `tmp/` stand for `/sdcard` and `/data/local/tmp`; `calls.log` records
every command line so a test can assert what was (or was not) run. Only the commands the adapter issues are understood.
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(os.environ["FAKE_ADB_STATE"])
STATE = ROOT / "state.json"


def load():
    return json.loads(STATE.read_text(encoding="utf-8"))


def save(state):
    STATE.write_text(json.dumps(state), encoding="utf-8")


def local(path):
    if path.startswith("/sdcard/"):
        return ROOT / "sdcard" / path[len("/sdcard/"):]
    if path.startswith("/data/local/tmp/"):
        return ROOT / "tmp" / path[len("/data/local/tmp/"):]
    return ROOT / "other" / path.lstrip("/")


def say(text, code=0):
    sys.stdout.write(text if text.endswith("\n") or not text else text + "\n")
    sys.stdout.flush()
    sys.exit(code)


def start_daemon():
    """Like the real `adb start-server`: leave a long-lived process behind that outlives this command, and record its pid in
    `server.pid` (one line per start). It is NOT moved out of the caller's process group or Windows job, as an adb server started
    inside a supervised call is not: whoever kills that call's tree kills the server too. It ends by itself after two minutes, so
    a test that forgets it does not leave it forever."""
    import subprocess
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    code = "import time; time.sleep(120)"
    if os.name == "nt":
        process = subprocess.Popen([sys.executable, "-c", code], creationflags=0x00000008, **options)  # DETACHED_PROCESS
    else:
        process = subprocess.Popen([sys.executable, "-c", code], **options)
    with open(ROOT / "server.pid", "a", encoding="utf-8") as record:
        record.write(f"{process.pid}\n")


def main(argv):
    with open(ROOT / "calls.log", "a", encoding="utf-8") as log:
        log.write(" ".join(argv) + "\n")
    state = load()
    if argv[:1] == ["start-server"]:
        if state.get("daemon"):
            start_daemon()
        say("")
    if state.get("down", 0) > 0:  # the shared adb server was restarted by another adb on the machine
        state["down"] -= 1
        save(state)
        say("* daemon not running; starting now at tcp:5037\nadb.exe: failed to check server version: cannot connect to daemon", 1)
    serial = None
    if argv[:1] == ["-s"]:
        serial, argv = argv[1], argv[2:]
    command, rest = argv[0], argv[1:]
    if command == "version":
        say("Android Debug Bridge version 1.0.41")
    if command == "devices":
        lines = ["adb server version (41) doesn't match this client (39); killing..."] if state.get("mismatch_notice") else []
        lines.append("List of devices attached")
        for d in state["devices"]:
            shown = d["state"]
            if d.get("offline_polls", 0) > 0:  # transient `offline` right after a server restart
                d["offline_polls"] -= 1
                shown = "offline"
                save(state)
            # Real adb prints no model for a device it cannot talk to.
            model = f" model:{d.get('model', 'Pixel_Fake')}" if shown == "device" else ""
            lines.append(f"{d['serial']}\t{shown} usb:1-1{model} transport_id:1")
        say("\n".join(lines))
    device = next((d for d in state["devices"] if d["serial"] == serial), None)
    if device is None:
        say(f"error: device '{serial}' not found", 1)
    if device.get("offline_polls", 0) > 0 and command != "devices":
        say("error: device offline", 1)
    if device["state"] == "unauthorized":
        say("error: device unauthorized.\nThis adbd's $ADB_VENDOR_KEYS is not set; try 'adb kill-server'.", 1)
    if device["state"] == "offline":
        say("error: device offline", 1)
    if command == "install":
        if state.get("install_error"):
            say(f"Performing Streamed Install\nadb: failed to install {rest[-1]}: Failure [{state['install_error']}]", 1)
        state["installed"] = sorted(set(state.get("installed", [])) | {state["package"]})
        save(state)
        say("Performing Streamed Install\nSuccess")
    if command == "push":
        source, target = rest
        destination = local(target)
        if source.endswith(os.sep + ".") or source.endswith("/."):
            shutil.copytree(source[:-2], destination, dirs_exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        say("1 file pushed")
    if command == "pull":
        source, target = rest
        if source.endswith("/."):
            shutil.copytree(local(source[:-2]), target, dirs_exist_ok=True)
        else:
            shutil.copyfile(local(source), target)
        say("files pulled")
    if command == "logcat":
        if "-c" in rest:
            say("")
        if state.get("no_logcat"):
            say("", 0)
        lines = state.get("logcat", [])
        for line in lines:
            print(line if "raw" in rest else f"10-29 12:00:00.000  1  1 I Unity   : {line}", flush=True)
        time.sleep(120)
        say("")
    if command == "shell":
        shell(state, device, " ".join(rest))
    say(f"unsupported fake adb command: {argv}", 2)


def shell(state, device, script):
    if script.startswith("'am' "):  # the adapter quotes every word of `am start`; the device shell would unquote them
        import shlex
        script = " ".join(shlex.split(script))
    package = state["package"]
    running = state.setdefault("running", [])
    if script.startswith("getprop"):
        say("\n".join(f"[{k}]: [{v}]" for k, v in device["props"].items()))
    if "dumpsys battery" in script:
        say("Current Battery Service state:\n  level: 77\n  temperature: 301\n  status: 2\n--thermal--\nThermal Status: 0\n"
            "--df--\nFilesystem 1K-blocks Used Available Use% Mounted on\n/dev/fuse 100000000 20000000 80000000 20% /sdcard")
    if script.startswith("dumpsys window") and device.get("no_keyguard_keys"):
        say("", 1)
    if script.startswith("dumpsys window"):
        say(f"    mShowingDream=false mDreamingLockscreen=false\n    isKeyguardShowing={'true' if device.get('locked') else 'false'}")
    if script.startswith("input keyevent") or "dismiss-keyguard" in script:
        device["locked"] = False if device.get("insecure") else device.get("locked", False)
        save(state)
        say("")
    if script.startswith("mkdir -p"):
        local(script.split("'")[1]).mkdir(parents=True, exist_ok=True)
        say("")
    if script.startswith("if [ -d"):
        path = local(script.split("'")[1])
        say("dir" if path.is_dir() else "file" if path.exists() else "absent")
    if script.startswith("pm list packages"):
        say(f"package:{package}" if package in state.get("installed", []) else "")
    if script.startswith("pm clear"):
        shutil.rmtree(local(f"/sdcard/Android/data/{package}"), ignore_errors=True)
        say("Success")
    if script.startswith("cmd package resolve-activity"):
        say(f"priority=0 preferredOrder=0\n{package}/com.unity3d.player.UnityPlayerGameActivity")
    if script.startswith("pidof"):
        say("4242" if package in running else "", 0 if package in running else 1)
    if script.startswith("dumpsys meminfo") or script.startswith("dumpsys gfxinfo"):
        say(script.split()[1] + " dump for " + package)
    if script.startswith("am force-stop"):
        if package in running:
            running.remove(package)
        save(state)
        say("")
    if script.startswith("am start"):
        if state.get("launch_error"):
            say("Error type 3\nError: Activity class does not exist.", 0)
        if package not in running:
            running.append(package)
        for relative, content in state.get("on_launch", {}).items():  # what the in-app harness writes after it starts
            target = local(f"/sdcard/Android/data/{package}/files/{relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(content), encoding="utf-8")
        save(state)
        say("Stopping: " + package + "\nStarting: Intent\nStatus: ok\nLaunchState: COLD")
    if script.startswith("screencap"):
        target = local(script.split()[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x89PNG fake")
        say("")
    if script.startswith("rm -f"):
        local(script.split()[-1]).unlink(missing_ok=True)
        say("")
    say(f"unsupported fake shell command: {script}", 2)


main(sys.argv[1:])
