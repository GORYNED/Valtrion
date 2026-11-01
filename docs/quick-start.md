# Quick Start

Install Valtrion and run your first supervised checks in about five minutes. No Unity, device or other dependency is needed. For a Unity project, continue with the [Unity Quick Start](unity-quick-start.md) afterwards.

You need Python 3.10 or newer.

## 1. Install

```sh
pipx install valtrion
valtrion --version
```

Expected: `valtrion 0.7.4 (contract 1)` (or a newer version). Without `pipx`, use a virtual environment: `python -m venv .venv`, then `.venv/bin/pip install valtrion` (`.venv\Scripts\pip` on Windows). Then run `.venv/bin/valtrion` (`.venv\Scripts\valtrion` on Windows) wherever this guide says `valtrion`, or activate the environment. To install from a checkout instead, see [installation](install.md).

## 2. Run a check

The commands below use Python so they work on every system. Replace the part after `--` with your own test or script.

```sh
valtrion job start --project demo --label hello --timeout-sec 30 --stall-sec 10 --watch -- python -c "print('hello')"
```

Valtrion prints the job directory, follows the events and ends with:

```text
GDV-RESULT PASS job=<id> offset=<n>
```

The exit code is 0. The job directory contains `job.json` (state and result), `events.jsonl` (what happened and when) and `console.log` (the command's output). Keep them as evidence.

On Windows, run these commands in PowerShell. Use `python3` instead of `python` if that is your command on macOS or Linux.

## 3. See a failure and a hang

A failing command ends as `FAIL`:

```sh
valtrion job start --project demo --label failing --timeout-sec 30 --stall-sec 10 --watch -- python -c "raise SystemExit(1)"
```

A command that stops making progress is stopped as `STALL`, after `--stall-sec` seconds without new output:

```sh
valtrion job start --project demo --label hang --timeout-sec 60 --stall-sec 5 --watch -- python -c "import time; time.sleep(600)"
```

A command that keeps running past `--timeout-sec` ends as `TIMEOUT`. The `valtrion` exit code is 0 for `PASS`, 1 for `FAIL`, 2 for `BLOCKED` and 3 for `STALL` or `TIMEOUT`. To check a job later, use `valtrion job status <job id>` and `valtrion job tail <job id>`.

A command job's `PASS` means the process exited 0, so your command must assert what you care about. It does not produce an app-side `gdv.result/1`, device evidence or metric comparison; those come from the Unity integration.

## 4. Use it on your project

Run your real test command with sensible limits and the revision it checks (in PowerShell or a POSIX shell, inside a Git repository; otherwise pass your own label to `--sha`):

```sh
valtrion job start --project my-app --sha "$(git rev-parse HEAD)" --label unit-tests --cwd . --timeout-sec 900 --stall-sec 120 --watch -- python -m pytest
```

`--sha` is a label you supply; it does not prove that the checkout is clean. A job can also hold a named resource lease (`--lease`) or run on a configured SSH host (`--host`); see the [CLI reference](cli.md) and the [trust model](trust-model.md) before using a remote host.

## 5. Update

```sh
pipx upgrade valtrion
valtrion --version
```

Read the [changelog](../CHANGELOG.md) first. On SSH hosts run `valtrion hosts sync` so every host has the same version.

## Next

- A longer example with a catalog check, invalid input and a timeout: [examples/python-check](../examples/python-check/README.md) (needs a checkout of this repository).
- Checking a Unity app: [Unity Quick Start](unity-quick-start.md). Unity is the only ready application integration. Other engines can use command jobs; see [architecture](ARCHITECTURE.md) and the [roadmap](../ROADMAP.md).
- Let an AI agent do the setup: [AI-agent setup](ai-agent-setup.md).
