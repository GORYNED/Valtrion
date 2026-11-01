# Valtrion Core

The Python CLI and library that drive Valtrion checks on local and SSH hosts.

Core handles host selection, resource locks, supervised jobs, platform providers, result collection and metric comparison. The runtime needs Python 3.10 or newer and uses only the standard library. Unity, device tools and project-defined build methods are required only for the checks that use them.

Use `valtrion job start -- ...` to supervise an ordinary command without Unity. It retains process status, events and console output, with timeouts and resource leases. See the Quick Start for a Python data check, including failure and timeout cases. Command jobs do not produce app-side plan results for `compare` or `report`.

Unity is the first and only ready application integration. The higher-level `run` and `pipeline` workflow, project discovery, build profiles and metric comparability still have Unity-specific assumptions. Platform providers are transport and lifecycle mechanisms, not proof of another engine integration.

This is pre-release software. Install with `pipx install valtrion` (update with `pipx upgrade valtrion`), or from a full repository checkout with `python tools/install.py --venv`. The Unity package is installed separately from Git with its own version; see the installation guide.

See the [repository README](https://github.com/GORYNED/Valtrion#readme), [Quick Start](https://github.com/GORYNED/Valtrion/blob/main/docs/quick-start.md), and [CLI reference](https://github.com/GORYNED/Valtrion/blob/main/docs/cli.md). Core and the Unity package have separate versions; the repository's `compat.json` defines compatible pairs.

Valtrion runs trusted project code with your permissions. See the [trust model](https://github.com/GORYNED/Valtrion/blob/main/docs/trust-model.md) before using it with a project or remote host.
