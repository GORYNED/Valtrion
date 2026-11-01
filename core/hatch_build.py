"""Bundle shared files from a checkout or reuse the copies in a source distribution."""

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root)
        sources = {
            "valtrion/_schemas": (root / "valtrion/_schemas", root.parent / "contracts/schemas"),
            "valtrion/_compat.json": (root / "valtrion/_compat.json", root.parent / "compat.json"),
            "valtrion/_license.txt": (root / "valtrion/_license.txt", root / "LICENSE", root.parent / "LICENSE"),
        }
        included = build_data.setdefault("force_include", {})
        for target, candidates in sources.items():
            source = next((path for path in candidates if path.exists()), None)
            if source is None:
                raise FileNotFoundError(f"Missing required distribution file: {target}")
            if source != root / target:
                included[str(source)] = target
        if self.target_name == "sdist":
            included[str(next(path for path in sources["valtrion/_license.txt"] if path.is_file()))] = "LICENSE"
