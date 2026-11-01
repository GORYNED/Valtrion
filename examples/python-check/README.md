# Python catalog check

Run an ordinary data check as a supervised Valtrion job, without Unity or an application integration.

The check rejects duplicate product SKUs and invalid quantities/prices, then calculates inventory value in cents. It uses Python's standard library. Valtrion starts the process, applies time and stall limits, and retains its exit status, output and events. No Core changes or Unity installation are needed.

## Run the check

From the repository root, install Core with `python tools/install.py --venv --home .valtrion-home`. Use the absolute CLI path printed by the installer if it differs from the examples below.

Windows PowerShell:

```powershell
$valtrion = (Resolve-Path .valtrion-home/.valtrion/core/0.7.4/Scripts/valtrion.exe).Path
$python = (Get-Command python).Source
$root = (Get-Location).Path
$sha = git rev-parse HEAD
$env:VALTRION_HOME = Join-Path $root '.valtrion-home/demo-state'
$env:GDV_WORK_ROOT = Join-Path $root '.valtrion-home/demo-work'
& $valtrion job start --project python-check --sha $sha --label catalog --cwd $root --timeout-sec 30 --stall-sec 10 --watch -- $python examples/python-check/check_catalog.py examples/python-check/catalog.json
$LASTEXITCODE
```

macOS / Linux shell:

```sh
VALTRION="$PWD/.valtrion-home/.valtrion/core/0.7.4/bin/valtrion"
PYTHON="$(python3 -c 'import sys; print(sys.executable)')"
ROOT="$PWD"
SHA="$(git rev-parse HEAD)"
export VALTRION_HOME="$ROOT/.valtrion-home/demo-state"
export GDV_WORK_ROOT="$ROOT/.valtrion-home/demo-work"
"$VALTRION" job start --project python-check --sha "$SHA" --label catalog --cwd "$ROOT" --timeout-sec 30 --stall-sec 10 --watch -- "$PYTHON" examples/python-check/check_catalog.py examples/python-check/catalog.json
echo "$?"
```

Expected: `GDV-RESULT PASS`, exit 0, and `Checked 2 products; inventory value 2150 cents` in the run's `console.log`. `--cwd` is required for these relative file paths: detached jobs otherwise use their run folder as the working directory. These environment variables persist in this shell; use a separate shell or restore previous values afterward.

## Inspect retained evidence

The first JSON object printed by `job start` gives `jobId` and the absolute run directory `dir`. Use that job ID with `valtrion job status JOB_ID` or `valtrion job tail JOB_ID` (the installed command path is still required). The directory contains:

- `job.json`: the terminal status and recorded command.
- `events.jsonl`: job, process and timeout events.
- `console.log`: the check's standard output and error.

Validate `job.json` and `events.jsonl` with `valtrion validate FILE`. Local evidence is already on this host; `collect` is for jobs on configured SSH hosts.

## Exercise failure and timeout

Repeat the same command with `invalid-catalog.json` instead of `catalog.json`. The duplicate SKU causes a real validation failure: `FAIL`, exit 1, and `duplicate sku: notebook` in `console.log`.

For the timeout path, keep the valid catalog, change `--timeout-sec 30` to `--timeout-sec 2`, and append `--delay-sec 20` after the catalog path. This deliberately delays input processing. Expect `TIMEOUT`, exit 3, a `timeout.detected` event, and termination of the check before it prints a successful count. Leave the stall limit at 10 seconds so this demonstrates the hard timeout.

Omit `--watch` to return after startup; later run `valtrion job watch JOB_ID`. Add `--lease catalog-check` before `--` if multiple jobs must share an exclusive resource. Arbitrary commands and leases are trusted configuration.

## What this establishes

This is useful process supervision and retained execution evidence. It does not implement a new app-side runner, execute `gdv.plan/1` steps, or produce `gdv.result/1`. A command job's `PASS` means that its process exited successfully; it proves only the assertions the check actually makes. `--sha` records the supplied revision label and does not attest to a clean checkout. App metric comparison, baseline promotion and result-based `report` need structured app results and are outside this example. Unity remains the only ready app integration.

The documented recipe was exercised on Windows / Python 3.14.4. The POSIX commands describe the same CLI interface; macOS and Linux execution is not verified for this candidate.

## Reproduce the installation and checks

From the repository root:

```sh
python examples/python-check/verify_example.py .verification/python-check
```

Choose a directory that does not exist. This verifier installs a fresh CLI offline, runs the three cases through the installed executable, checks actual exit codes and retained output, and validates job/event contracts. It uses bounded subprocess waits and leaves raw logs plus `summary.json` under that directory for inspection. No Unity configuration or existing Valtrion state is used. Do not commit raw logs: they include local paths and host names.
