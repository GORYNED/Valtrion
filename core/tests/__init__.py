"""Core tests. Callers of the pre-start Unity check (the build stage, the Editor provider, `unity run`) must not depend on the Editors and
the memory of the machine the tests run on, so the default probe is a quiet one; tests of the check itself name their own probe."""

import os

from valtrion import unityguard

# Tests never touch the real copies of installed apps that Apple's device service keeps on the machine they run on.
os.environ.setdefault("VALTRION_DEVICE_INSTALL_CACHE", "")


class QuietProbe(unityguard.Probe):
    os_name = "linux"

    def processes(self):
        return []

    def free_memory_mib(self):
        return 1.0e6


unityguard.probe_factory = QuietProbe
