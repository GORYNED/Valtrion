# Changelog: Valtrion Core

Core (the `valtrion` Python package) and the Unity package `com.goryned.valtrion.unity` are versioned independently. The Unity changelog is in `integrations/unity/com.goryned.valtrion.unity/CHANGELOG.md`; `compat.json` lists compatible pairs.

## 0.7.4

First release on PyPI (`pipx install valtrion`), published as a GitHub pre-release.

- Packaging: removed the `Private :: Do Not Upload` classifier, added Beta status, Python versions and project URLs. No runtime change.
- Releases are made by the manual Release workflow with PyPI Trusted Publishing; see `docs/releasing.md`.
