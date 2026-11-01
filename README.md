# Valtrion

Run repeatable checks, stop hung work, and keep results and logs you can inspect.

Valtrion gives a test command or an application smoke check a managed run: a time limit, progress monitoring, an outcome and saved evidence. Use it when you need to repeat a check after a change and explain what actually ran.

## How it works

1. Choose your check: an ordinary command, or a JSON plan for the Unity integration.
2. Start it with the Python CLI on this machine or a configured SSH host.
3. Valtrion supervises execution, stops a timeout or stall, and saves the outcome and logs.
4. Inspect the evidence. Unity plan results can also include steps, screenshots and metrics for comparison with a compatible baseline.

Your check supplies the assertions. A command job's PASS means exit zero; it saves job state, events and console output. It does not automatically produce an app-side `gdv.result/1` or device evidence.

## Start here

**No Unity needed:** follow the [Core Quick Start](docs/quick-start.md). It installs the CLI and runs a Python data check, including a real failure and timeout.

**Checking a Unity app:** follow the [Unity Quick Start](docs/unity-quick-start.md). Unity is the first and currently only ready application integration. Its package executes plan steps inside the Editor or a development player.

## Install

Core (Python 3.10+, standard library only) from PyPI (available from the first release, Core 0.7.4; before that use the checkout installer in [installation](docs/install.md)):

```sh
pipx install valtrion
valtrion --version
```

The Unity package is installed separately, with its own version, from Git. In Unity: Window → Package Manager → + → Add package from git URL, or add to `Packages/manifest.json`:

```json
"com.goryned.valtrion.unity": "https://github.com/GORYNED/Valtrion.git?path=integrations/unity/com.goryned.valtrion.unity#unity-v0.7.1"
```

Update Core with `pipx upgrade valtrion` and the Unity package by changing the tag in that URL; `valtrion doctor --project <Unity project>` checks that the pair is compatible ([compat.json](compat.json)). See [installation](docs/install.md) (including installing from a checkout and SSH hosts), or give an agent the [setup prompt](docs/ai-agent-setup.md), which chooses the path for your project. Versions and how releases are made: [releasing](docs/releasing.md).

## Available now

| Area | What it provides |
| --- | --- |
| Core command jobs | Local or explicitly configured SSH execution, timeouts, stall detection, resource leases and retained logs/events |
| Unity integration | Built-in and custom C# steps, heartbeat, structured results, metrics and screenshots |
| App pipelines | Build, install, run and collect through Editor, Android, iOS, Windows and macOS providers, with project-supplied build/test entry points |
| Reviewing a change | Check selection, app result reports and metric comparison with compatible baselines |

Providers handle platform tools and lifecycle; they are not integrations for other engines. Core 0.7.4 and Unity package 0.7.1 have separate versions, paired by [compat.json](compat.json). The Unity package requires 6000.6 or newer; the verification project uses 6000.6.7f1.

## Status and limits

Valtrion is pre-release software under the [MIT license](LICENSE). Releases are GitHub Pre-releases (Core also on PyPI); the CLI and API may change before a stable release. The [release-readiness record](docs/release-readiness.md) names tested environments, evidence and remaining gaps.

- Adding the Unity package does not create project build methods or an Editor provider runner. The Unity Quick Start first checks the package directly through Test Framework.
- Project discovery, profiles and app metric comparison still have Unity assumptions. Other engines have no ready in-app runner; there is no Linux player provider.
- Standalone `build`, `install`, `launch`, `push`, `pull`, `screenshot` and `logs` commands are placeholders. Use configured `run` or `pipeline` workflows for app checks.
- Checks run trusted code with your permissions. Read the [trust model](docs/trust-model.md) before using a project or remote host.

## More detail

[Overview](docs/overview.md) · [CLI](docs/cli.md) · [Providers](docs/providers.md) · [Unity package](integrations/unity/com.goryned.valtrion.unity/Documentation~/index.md) · [Contracts](docs/contracts.md) · [Protocol](docs/protocol.md) · [Local files and jobs](docs/work-root.md) · [Architecture](docs/ARCHITECTURE.md) · [Roadmap](ROADMAP.md)

For development and test commands, see [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities through [SECURITY.md](SECURITY.md).
