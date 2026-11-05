"""`valtrion dashboard`: start the dashboard (once) and open it.

Without options the command makes sure a dashboard process runs for this user (on Windows with its tray icon), then opens the page in the
browser and returns, so it can be bound to a shortcut. `--foreground` is that process itself."""
from __future__ import annotations

import http.client
import json
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import TextIO

from . import core, dashboard, dashboardserver, nowindow, remote, tray

START_WAIT_SEC = 15.0
CREATE_NO_WINDOW, CREATE_NEW_PROCESS_GROUP = 0x08000000, 0x00000200


def running(home: Path) -> dict | None:
    """The state of the dashboard process that answers on its port, else None."""
    state = dashboardserver.read_state(home)
    if state is None:
        return None
    try:
        connection = http.client.HTTPConnection("127.0.0.1", int(state["port"]), timeout=2)
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        response.read()
        connection.close()
    except (OSError, ValueError, http.client.HTTPException):
        return None
    return state if response.status == 200 else None


def _python_for_background() -> str:
    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")
    return str(windowless if os.name == "nt" and windowless.is_file() else python)


def start_background(home: Path, port: int, no_tray: bool) -> dict | None:
    argv = [_python_for_background(), "-I", "-c", remote.LAUNCHER, str(remote.CLI_ROOT), "dashboard", "--foreground", "--port", str(port)]
    if no_tray:
        argv.append("--no-tray")
    options = {"creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **options)
    deadline = time.monotonic() + START_WAIT_SEC
    while time.monotonic() < deadline:
        state = running(home)
        if state is not None:
            return state
        time.sleep(0.25)
    return None


def stop_running(home: Path, out: TextIO) -> int:
    state = dashboardserver.read_state(home)
    if state is None:
        print("The dashboard is not running.", file=out)
        return 0
    pid = int(state.get("pid", 0))
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=30, check=False, **nowindow.OPTIONS)
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    dashboardserver.state_path(home).unlink(missing_ok=True)
    print("The dashboard was stopped.", file=out)
    return 0


def foreground(home: Path, port: int, no_tray: bool, open_page: bool, out: TextIO) -> int:
    try:
        server, state = dashboardserver.start(home, port)
    except OSError as error:
        print(f"Cannot start the dashboard: {error}", file=out)
        return 2
    url = dashboardserver.url_for(state)
    try:
        if open_page:
            webbrowser.open(url)
        if os.name == "nt" and not no_tray:
            try:
                icon = tray.write_icon(home)
                tray.Tray("Valtrion", icon, lambda: webbrowser.open(url), lambda: None).run()
                return 0
            except OSError as error:  # no desktop for an icon (a service session): the page still works
                print(f"No tray icon: {error}", file=out)
        print(f"Valtrion dashboard: {url}", file=out)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        return 0
    finally:
        dashboardserver.stop(server, home)


def run(arguments, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    home = core.valtrion_home()
    if arguments.snapshot:
        print(json.dumps(dashboard.Collector(home).snapshot()), file=out)
        return 0
    if arguments.stop:
        return stop_running(home, out)
    if arguments.foreground:
        return foreground(home, arguments.port, arguments.no_tray, arguments.open, out)
    state = running(home)
    if state is None:
        if os.name != "nt":  # no tray here: the dashboard runs in this terminal until Ctrl+C
            return foreground(home, arguments.port, True, not arguments.no_open, out)
        state = start_background(home, arguments.port, arguments.no_tray)
        if state is None:
            print("The dashboard did not start in time.", file=out)
            return 2
    url = dashboardserver.url_for(state)
    print(f"Valtrion dashboard: {url}", file=out)
    if not arguments.no_open:
        webbrowser.open(url)
    return 0
