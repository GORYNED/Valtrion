"""Core tests. Callers of the pre-start Unity check (the build stage, the Editor provider, `unity run`) must not depend on the Editors and
the memory of the machine the tests run on, so the default probe is a quiet one; tests of the check itself name their own probe."""

from valtrion import unityguard


class QuietProbe(unityguard.Probe):
    os_name = "linux"

    def processes(self):
        return []

    def free_memory_mib(self):
        return 1.0e6


unityguard.probe_factory = QuietProbe
