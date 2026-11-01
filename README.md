# Valtrion

Repeatable checks, managed execution and evidence you can inspect and share.

Valtrion helps you run the same checks again, control their execution and keep the evidence behind the outcome. Core can supervise an ordinary command without Unity: start a job, set time and progress limits, hold resource locks, and retain its exit status, events and console output.

Unity is the first and currently only ready application integration. Its package executes JSON plans inside the Editor or a development player and produces structured steps, logs and metrics. Use it for smoke checks, app-specific scenarios, performance measurements and longer runs. Run checks yourself or through an AI coding agent; no particular agent is required.

## How it works

1. Define a repeatable command, or use the Unity package to define an app-side plan.
2. Run it with the Python CLI, locally or on a host you configure over SSH.
3. Core watches execution and saves evidence. Missing tools or permissions do not count as a pass.
4. Inspect the outcome and files. Unity plan results also support metric comparison with compatible baselines.

Command jobs record process outcomes; they do not automatically turn console output into app assertions or `gdv.result/1`. The Unity package runs the steps inside your app. Platform providers handle builds, device access and file transport; they are not integrations for other engines. Editor, emulator, host and physical-device plan results carry different evidence labels.

## Start here

- [Quick Start](docs/quick-start.md): install Core and run a useful Python check without Unity.
- [Unity Quick Start](docs/unity-quick-start.md): add the package and run an Editor smoke check.
- [AI-agent setup prompt](docs/ai-agent-setup.md): a ready-to-use prompt for Claude Code, Codex, Copilot and other coding agents.
- [Installation](docs/install.md): install from this repository and configure additional hosts.

You need Python 3.10 or newer and Git for the CLI. Unity checks also need a licensed Editor matching your project. The Unity package declares Unity 6000.6 as its minimum; the repository test project specifies 6000.6.7f1. Device runs need the platform's tools, build modules and a connected, authorized target.

From a repository checkout, install the CLI with:

```sh
python tools/install.py --venv
```

Use the absolute `valtrion` command path printed by the installer, or add its directory to your `PATH`. See the Quick Start for an ordinary command job; no Unity installation or project configuration is needed.

## What is available

| Area | Current implementation |
| --- | --- |
| Core command checks | Supervised local or configured SSH commands, process status, retained logs/events and resource leases; no Unity required |
| Unity integration | Development-only runner, built-in steps, custom C# steps, metrics, screenshots, heartbeat and JSON results |
| Platform providers | Unity Editor, Android, iOS, Windows and macOS; the documented app workflow uses Unity and needs platform tools and project configuration |
| Multiple machines | Explicit SSH host inventory, host selection, detached jobs and result collection |
| Repeatable checks | JSON plans, build/run pipelines, time limits, stall detection and resource locks |
| Review evidence | Check selection from changed files, result reports and metric comparison with compatible baselines |

The Core runtime uses only Python's standard library. Core 0.7.4 and Unity package 0.7.1 have separate versions; [compat.json](compat.json) records their compatibility.

## Status and limits

Valtrion is pre-release software under the [MIT license](LICENSE). Install from a checkout; no package-index install or published release artifact is required by these instructions. The CLI and API may change before a stable release.

- Your project supplies build methods and, for the Editor provider, a Python test runner and plan test assembly. Adding the package alone does not create them.
- Implemented providers are not a guarantee that every platform or device has been tested for the current commit. See the [release-readiness record](docs/release-readiness.md) for actual checks and blockers.
- Only Unity has an app-side runner. There is no Linux player provider.
- The standalone CLI commands `build`, `install`, `launch`, `push`, `pull`, `screenshot` and `logs` are placeholders. Use `run` or `pipeline` for a configured validation run.
- Valtrion runs trusted project code with your permissions. Read the [trust model](docs/trust-model.md) before adding a project or remote host.

## Documentation and development

| Guide | What it covers |
| --- | --- |
| [Overview](docs/overview.md) | A run from plan to result |
| [CLI reference](docs/cli.md) | Commands and exit codes |
| [Providers](docs/providers.md) | Platform requirements and build profiles |
| [Unity package guide](integrations/unity/com.goryned.valtrion.unity/Documentation~/index.md) | C# steps, probes and test integration |
| [Contracts](docs/contracts.md) and [protocol](docs/protocol.md) | JSON formats and host/app communication |
| [Local files and jobs](docs/work-root.md) | State, temporary work, locks and cleanup |
| [Architecture](docs/ARCHITECTURE.md) | Components and boundaries |
| [Roadmap](ROADMAP.md) | Implemented features, gaps and intended work |

To contribute, read [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities using [SECURITY.md](SECURITY.md).

From the repository root, check the contracts with `python tools/check_contracts.py`. Run Core tests with:

```sh
cd core
python -m unittest discover -s tests -t .
```
