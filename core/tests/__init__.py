"""Core tests. Callers of the pre-start Unity check (the build stage, the Editor provider, `unity run`) must not depend on the Editors and
the memory of the machine the tests run on, so the default probe is a quiet one; tests of the check itself name their own probe."""

import os

from valtrion import unityguard

# Tests never touch the real copies of installed apps that Apple's device service keeps on the machine they run on.
os.environ.setdefault("VALTRION_DEVICE_INSTALL_CACHE", "")
# A `unity run` of a test never hands itself to the session of the logged-in user: on a Windows host reached over SSH it would create a
# real Task Scheduler task (and a console on that user's screen). The tests of that hand-over pass their own scheduler.
os.environ.setdefault("GDV_USER_SESSION", "1")


class QuietProbe(unityguard.Probe):
    os_name = "linux"

    def processes(self):
        return []

    def free_memory_mib(self):
        return 1.0e6


unityguard.probe_factory = QuietProbe
