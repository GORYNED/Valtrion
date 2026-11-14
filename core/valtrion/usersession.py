"""Run a command in the interactive session of the logged-in user (Windows).

A process started over SSH or by a service lives in a window station that has no display. A Unity Editor there starts and runs batch
tests, but the first test that opens an `EditorWindow` makes it create a swap chain for that window, which the system refuses
(`d3d12: CreateSwapChainForHwnd failed (887a0022)`), and the Editor ends with `Unrecoverable GPU device error`. The same command started
by the user at the desktop passes.

`run` starts the command again through the Task Scheduler as the current user, interactive and elevated like the SSH session, so it
runs in the console session (still on its own hidden desktop; see `hiddendesktop.py`). The output of the command is copied to this
process while it goes on, its exit code is returned, the task is deleted at the end. When nobody is logged in, or the task does not
start, `run` returns None and the caller runs the command here as before."""
from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Sequence, TextIO

from . import core
from .providers.unity import support

#: Set in the command that runs in the user's session, so that it does not start itself in another one.
ENVIRONMENT_VARIABLE = "GDV_USER_SESSION"
#: How long the command has to show that it started; a task of a session that nobody is logged in to never does.
START_WAIT_SEC = 45.0
POLL_SEC = 0.5
STOP_WAIT_SEC = 15.0

Runner = Callable[[Sequence[str]], "tuple[int, str]"]


def needed(desktop: Callable[[], bool | None] = support.interactive_desktop, environ: dict | None = None) -> bool:
    """Whether this process should hand a command that draws windows to the user's session: Windows, not already on an interactive
    desktop (`False`; unknown is not), and not itself the command that was handed over."""
    env = os.environ if environ is None else environ
    return core.local_os() == "windows" and desktop() is False and not env.get(ENVIRONMENT_VARIABLE)


def command(arguments: Sequence[str]) -> list[str]:
    """This program with `arguments`: the `valtrion` executable that is running, else the module of this interpreter."""
    program = Path(sys.argv[0])
    if program.suffix.lower() == ".exe" and program.exists():
        return [str(program.resolve()), *arguments]
    return [sys.executable, "-m", "valtrion", *arguments]


def script(argv: Sequence[str], folder: Path) -> str:
    """The batch file of the task: marks the start, runs `argv` with its output in `out.txt`, writes the exit code into `exit.txt`."""
    quoted = subprocess.list2cmdline([str(item) for item in argv]).replace("%", "%%")
    home = os.environ.get(core.HOME_ENV)  # a state folder chosen for this process is the one the command in the session must use
    return "\r\n".join([
        "@echo off",
        f"set {ENVIRONMENT_VARIABLE}=1",
        *([f'set "{core.HOME_ENV}={home}"'] if home else []),
        f'echo started> "{folder / "started.txt"}"',
        f'{quoted} > "{folder / "out.txt"}" 2>&1',
        f'echo %errorlevel%> "{folder / "exit.txt"}"',
        ""])


def _schtasks(arguments: Sequence[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(["schtasks", *arguments], capture_output=True, text=True, errors="replace", timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return 127, str(error)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _user() -> str:
    """The account name as the Task Scheduler resolves it for this account; the domain of an SSH session's environment is not always one it
    knows ("No mapping between account names and security IDs"), the plain name is."""
    return os.environ.get("USERNAME") or getpass.getuser()


def run(argv: Sequence[str], out: TextIO, err: TextIO, cancelled: Callable[[], bool] = lambda: False, schtasks: Runner = _schtasks,
        sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic, home: Path | None = None,
        user: str | None = None) -> int | None:
    """The exit code of `argv` run in the interactive session, its output copied to `out`; None when it could not be started there
    (the reason is written to `err`). `cancelled()` true: the task is stopped and 1 is returned."""
    who = user or _user()
    if not who:
        return None
    folder = (home or core.valtrion_home()) / "session" / uuid.uuid4().hex[:12]
    name = "valtrion-" + folder.name
    try:
        folder.mkdir(parents=True, exist_ok=True)
        batch = folder / "run.cmd"
        batch.write_text(script(argv, folder), encoding="utf-8", newline="")
    except OSError as error:
        print(f"valtrion: no user session ({error})", file=err)
        return None
    created = False
    try:
        # The task is only a way to start the command in the session; it is run at once with /Run, so its own time never comes. A time in the
        # past is refused, hence one a little ahead.
        later = time.strftime("%H:%M", time.localtime(time.time() + 600))
        code, text = schtasks(["/Create", "/TN", name, "/TR", f'"{batch}"', "/SC", "ONCE", "/ST", later, "/IT", "/RL", "HIGHEST", "/RU", who, "/F"])
        if code != 0 and schtasks(["/Query", "/TN", name])[0] == 0:
            code = 0  # created, with a warning (a time that has passed when midnight comes between)
        if code != 0:
            print(f"valtrion: the command is run in this session, the user session is not available ({text.strip()[:200]})", file=err)
            return None
        created = True
        code, text = schtasks(["/Run", "/TN", name])
        if code != 0:
            print(f"valtrion: the command is run in this session, the user session did not start it ({text.strip()[:200]})", file=err)
            return None
        return _follow(name, folder, out, err, cancelled, schtasks, sleep, monotonic)
    finally:
        if created:
            schtasks(["/Delete", "/TN", name, "/F"])
        shutil.rmtree(folder, ignore_errors=True)


def _follow(name: str, folder: Path, out: TextIO, err: TextIO, cancelled: Callable[[], bool], schtasks: Runner,
            sleep: Callable[[float], None], monotonic: Callable[[], float]) -> int | None:
    started, finished, output = folder / "started.txt", folder / "exit.txt", folder / "out.txt"
    begin, offset, stopping = monotonic(), 0, None

    def relay() -> None:
        nonlocal offset
        try:
            with output.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read()
        except OSError:
            return
        if chunk:
            offset += len(chunk)
            out.write(chunk.decode("utf-8", errors="replace"))
            out.flush()

    while True:
        relay()
        if finished.exists():
            relay()
            try:
                return int(finished.read_text(encoding="ascii", errors="replace").strip())
            except ValueError:
                return 1
        if stopping is None and cancelled():
            schtasks(["/End", "/TN", name])
            stopping = monotonic()
            print("valtrion: the caller is gone, the command in the user session is stopped", file=err)
        if stopping is not None and monotonic() - stopping > STOP_WAIT_SEC:
            return 1
        if stopping is None and not started.exists() and monotonic() - begin > START_WAIT_SEC:
            schtasks(["/End", "/TN", name])
            print("valtrion: the command is run in this session, nobody is logged in to start it in", file=err)
            return None
        sleep(POLL_SEC)
