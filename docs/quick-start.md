# Quick Start: Core without Unity

Run a real Python data check as a supervised job. Core retains the command's
status, events and console output and enforces time and progress limits. You
need Git and Python 3.10+ with `venv` and `pip`. No Unity Editor, package, project,
device or third-party runtime dependency is needed.

## Install from PyPI

If Core is published on PyPI, `pipx install valtrion` gives you the `valtrion` command; skip to the check below and use `valtrion` in place of `$valtrion`/`$VALTRION`, with the example files from a checkout of this repository. Otherwise, or to run exactly a checkout's code:

## Install from this repository

Public HTTPS cloning needs no GitHub account or token. Private forks require your existing Git access. From a new checkout:

```sh
git clone https://github.com/GORYNED/Valtrion.git
cd Valtrion
git rev-parse HEAD
python tools/install.py --venv --home .valtrion-home
```

Record the resolved commit. Use `python3` if that is your Python 3 command. The
installer works offline and prints the absolute CLI path. No environment
activation is required. This checkout installs Core 0.7.4; use the printed path
if the version or location differs.

```powershell
# Windows PowerShell, from the checkout root
$valtrion = (Resolve-Path .valtrion-home/.valtrion/core/0.7.4/Scripts/valtrion.exe).Path
& $valtrion --version
& $valtrion job start --help
```

```sh
# macOS / Linux, from the checkout root
VALTRION="$PWD/.valtrion-home/.valtrion/core/0.7.4/bin/valtrion"
"$VALTRION" --version
"$VALTRION" job start --help
```

Expected version: `valtrion 0.7.4 (contract 1)`. The POSIX commands describe
the implemented host path; see [release readiness](release-readiness.md) for
the environments actually exercised in this preparation.

## Run a useful check

From the checkout root, run the included catalog validator:

```powershell
$python = (Get-Command python).Source
$root = (Get-Location).Path
$sha = git rev-parse HEAD
$env:VALTRION_HOME = Join-Path $root '.valtrion-home/demo-state'
$env:GDV_WORK_ROOT = Join-Path $root '.valtrion-home/demo-work'
& $valtrion job start --project python-check --sha $sha --label catalog --cwd $root --timeout-sec 30 --stall-sec 10 --watch -- $python examples/python-check/check_catalog.py examples/python-check/catalog.json
$LASTEXITCODE
```

```sh
PYTHON="$(python3 -c 'import sys; print(sys.executable)')"
ROOT="$PWD"
SHA="$(git rev-parse HEAD)"
export VALTRION_HOME="$ROOT/.valtrion-home/demo-state"
export GDV_WORK_ROOT="$ROOT/.valtrion-home/demo-work"
"$VALTRION" job start --project python-check --sha "$SHA" --label catalog --cwd "$ROOT" --timeout-sec 30 --stall-sec 10 --watch -- "$PYTHON" examples/python-check/check_catalog.py examples/python-check/catalog.json
echo "$?"
```

Expected: `GDV-RESULT PASS`, exit 0, and `Checked 2 products; inventory value
2150 cents` in the run's `console.log`. `--cwd` makes the relative paths resolve
in this checkout. The environment variables persist: use a separate shell or
restore their previous values afterward. `--sha` is a supplied revision label,
not an attestation that your checkout is clean.

Continue with [examples/python-check](../examples/python-check/README.md).
It validates a small catalog's data, then demonstrates a genuinely
invalid input and an intentionally slow process. All three use the installed
CLI: expected outcomes are PASS (0), FAIL (1) and TIMEOUT (3).

The example provides exact PowerShell and POSIX commands, isolated state paths,
an explicit working directory and inspection commands. Keep the printed job IDs
and run directories: inspect `job.json`, `events.jsonl` and `console.log` after
each run. Status alone does not explain which assertion ran; the console and
events carry that evidence. Apply the same interface to your own trusted test
command, with appropriate limits.

## What this proves

Core can manage useful checks without an application integration. A command job
can also hold a named resource lease or execute on an explicitly configured SSH
host; this example verifies local execution only. See the [CLI reference](cli.md)
and [installation guide](install.md) before configuring a remote host.

A command job's PASS means the process exited zero. It does not create a
`gdv.result/1`, run JSON plan steps inside an application, provide device evidence,
or feed app metric comparison and verification reports. The check itself must
assert the behavior you care about. No successful app result is fabricated here.

## Application checks

Unity is the first and only ready application integration. Follow the separate
[Unity Quick Start](unity-quick-start.md) to add the package and run a plan in the
Editor. That guide includes full installation, smoke steps and evidence checks;
its runtime verification status is recorded in [release readiness](release-readiness.md).

Other engines and applications can use an existing external command check when
that meets the task. They do not yet have a ready in-app integration. Project
discovery, profiles and metric comparison still contain Unity assumptions; see
[architecture](ARCHITECTURE.md) and [roadmap](../ROADMAP.md). The
[AI-agent prompt](ai-agent-setup.md) chooses the appropriate path before changing
a project.
