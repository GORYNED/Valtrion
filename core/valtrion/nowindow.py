"""Keyword arguments that keep a console program started by valtrion from opening a console window on Windows.

A background runner has no console (`DETACHED_PROCESS`), so every console child it starts (git, ssh, python, taskkill) would
otherwise get a new visible window. Spread `**nowindow.OPTIONS` into `subprocess.run` / `subprocess.Popen`; on other systems it is empty."""
from __future__ import annotations

import os

CREATE_NO_WINDOW = 0x08000000

OPTIONS: dict = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
