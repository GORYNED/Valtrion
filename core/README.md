# Valtrion Core

The Python CLI and library that drive Valtrion checks on local and SSH hosts.

Core handles host selection, resource locks, supervised jobs, platform providers, result collection and metric comparison. The runtime needs Python 3.10 or newer and uses only the standard library. Unity, device tools and project-defined build methods are required only for the checks that use them.

This is pre-release software. Install from the full repository checkout with `python tools/install.py --venv`; use the command path printed by the installer. The checkout includes the schemas and compatibility table needed by the installed package.

See the [repository README](https://github.com/GORYNED/Valtrion#readme), [Quick Start](https://github.com/GORYNED/Valtrion/blob/main/docs/quick-start.md), and [CLI reference](https://github.com/GORYNED/Valtrion/blob/main/docs/cli.md). Core and the Unity package have separate versions; the repository's `compat.json` defines compatible pairs.

Valtrion runs trusted project code with your permissions. See the [trust model](https://github.com/GORYNED/Valtrion/blob/main/docs/trust-model.md) before using it with a project or remote host.
