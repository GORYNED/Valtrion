# Unity Quick Start

Install the CLI, add the Unity package to a project, then run a small plan in the Editor. This checks the integration; it does not test your game's behavior or prove anything about a physical device.

You need Git, Python 3.10 or newer with `venv` and `pip`, and a licensed Unity Editor matching your project's `ProjectSettings/ProjectVersion.txt`. The package declares Unity 6000.6 or newer. See [release readiness](release-readiness.md) for the versions actually tested. Public HTTPS cloning and UPM Git installation need no GitHub account or token; private forks require existing access.

## 1. Install the CLI

Install Core from PyPI:

```sh
pipx install valtrion
valtrion --version
```

The smoke plan and test assets used below live in this repository, so clone it next to your Unity project, outside its `Assets` folder. Check out the release tag that matches the package you install, so the examples and package agree:

```sh
git clone https://github.com/GORYNED/Valtrion.git
cd Valtrion
git checkout unity-v0.18.0
```

Record the resolved commit with `git rev-parse HEAD`. To test unreleased code instead, install Core from the checkout with `python tools/install.py --venv --home .valtrion-home` (use `python3` if that is your Python 3 command; the last line it prints is the CLI path) and use that same checkout for the package.

With the PyPI install, check the command from the checkout root:

```sh
valtrion --version
valtrion validate examples/smoke.plan.json
valtrion check-config --checks examples/quick-start-checks.json
```

Expected: `valtrion 0.7.4 (contract 1)` (or a newer version that [compat.json](../compat.json) pairs with the package), a valid `gdv.plan/1` and a valid check map. If you installed from the checkout, run the same commands with the CLI path the installer printed (no environment activation or `PATH` change needed).

## 2. Add the Unity package

In the Unity project, merge the following into `Packages/manifest.json`. Preserve existing dependencies:

```json
{
  "dependencies": {
    "com.goryned.valtrion.unity": "https://github.com/GORYNED/Valtrion.git?path=integrations/unity/com.goryned.valtrion.unity#unity-v0.18.0",
    "com.unity.test-framework": "1.8.0"
  }
}
```

The package version is pinned by the release tag, so the project stays reproducible. You can also add the same URL in Window → Package Manager → + → Add package from git URL, then keep `com.unity.test-framework`. Keep an existing compatible Test Framework version; 1.8.0 is the repository's tested version. Since package 0.11.0 (the tag above is 0.11.2) the helper assembly `Valtrion.Unity.PlayModeTests` is an ordinary package assembly: leave out `testables`, and your test runs contain only your own tests. Projects that stay on package 0.10.x still need `"testables": ["com.goryned.valtrion.unity"]` for the test helper used below, which also brings the package's own tests into your test runs. When you upgrade an existing project to 0.11.0, remove `com.goryned.valtrion.unity` from `testables` (and the key if it is then empty) and drop any exclusion of `Valtrion.Unity.Tests` classes such as `ContractFixtureTests`; asmdef references to `Valtrion.Unity.PlayModeTests` and `editor.planAssembly` stay unchanged. To try unreleased code, point to your checkout instead with `"file:/absolute/path/to/Valtrion/integrations/unity/com.goryned.valtrion.unity"` (forward slashes; `file:C:/...` on Windows) and do not commit that absolute path. To pin an exact commit instead of a tag, use its full SHA:

```text
https://github.com/GORYNED/Valtrion.git?path=integrations/unity/com.goryned.valtrion.unity#<full-reviewed-commit-sha>
```

This is a URL template: replace the SHA. Private repository credentials must come from your normal Git credential manager, never the manifest. Core and Unity versions are different; their compatibility is recorded in [compat.json](../compat.json).

## 3. Add the smoke test and plan

Copy both files from [examples/unity-smoke](../examples/unity-smoke) into `Assets/Validation/` in your Unity project. The assembly is named `Valtrion.SmokeTests`. Copy [examples/smoke.plan.json](../examples/smoke.plan.json) to `validation/smoke.plan.json` in your Unity project.

The test calls `PlanTest.RunFromEnvironment()`. The plan waits a few frames and writes a marker. It uses only built-in steps; add app-specific assertions later. It does not need a device, signing identity or build method.

Optionally create `valtrion.json` in your project's Git root:

```json
{ "unityProject": ".", "checks": "validation/checks.json" }
```

If Unity lives in a subfolder, replace `.` with its relative path. Copy [examples/quick-start-checks.json](../examples/quick-start-checks.json) to the `checks` location relative to this file. Check it with `valtrion check-config --checks validation/checks.json` using your installed command path. This initial map selects the smoke assembly for all changed paths. Refine it as your checks grow. `select` chooses checks; it does not execute them.

## 4. Run in a background Editor

If another Editor uses this project, wait until its owner closes it. Stop only an Editor you started. Before launching a test Editor, check the machine's other batchmode test runs and wait for the available test slot. Choose the actual Editor executable, project path, plan path and output directory. Use an output directory outside `Assets` and ignore it in Git. In these examples, run from your Unity project root.

```powershell
$editor = 'C:/Program Files/Unity/Hub/Editor/6000.6.7f1/Editor/Unity.exe'
$project = (Get-Location).Path
$out = Join-Path $project '.valtrion-results'
New-Item -ItemType Directory -Force $out | Out-Null
$runId = 'smoke-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
$previous = $env:GDV_PLAN, $env:GDV_OUT_ROOT, $env:GDV_RUN_ID
try {
    $env:GDV_PLAN = (Resolve-Path validation/smoke.plan.json).Path
    $env:GDV_OUT_ROOT = $out
    $env:GDV_RUN_ID = $runId
    $process = Start-Process -FilePath $editor -WindowStyle Hidden -PassThru -ArgumentList "-batchmode -projectPath `"$project`" -runTests -testPlatform PlayMode -assemblyNames Valtrion.SmokeTests -testResults `"$out/$runId.xml`" -logFile `"$out/$runId.log`""
    $process.WaitForExit()
    $process.ExitCode
} finally {
    $env:GDV_PLAN, $env:GDV_OUT_ROOT, $env:GDV_RUN_ID = $previous
}
```

```sh
EDITOR='/Applications/Unity/Hub/Editor/6000.6.7f1/Unity.app/Contents/MacOS/Unity'
PROJECT="$PWD"
OUT="$PROJECT/.valtrion-results"
mkdir -p "$OUT"
RUN_ID="$(python3 -c 'import uuid; print("smoke-" + uuid.uuid4().hex[:12])')"
GDV_PLAN="$PROJECT/validation/smoke.plan.json" GDV_OUT_ROOT="$OUT" GDV_RUN_ID="$RUN_ID" \
  "$EDITOR" -batchmode -projectPath "$PROJECT" -runTests -testPlatform PlayMode \
  -assemblyNames Valtrion.SmokeTests -testResults "$OUT/$RUN_ID.xml" -logFile "$OUT/$RUN_ID.log"
```

Replace the Editor path/version with the one your project requires; on Linux use its installed Unity binary. These are Unity Test Framework commands, not a dependency on the separate Unity CLI. Do not add `-quit`: it can exit before tests finish. Watch the run's `.log` during startup; stop only your own process tree if it stalls. For repeatable automated runs, use a supervisor with a hard timeout and a no-progress limit. The snippets generate a fresh run id to avoid accepting old results.

Verify all three pieces of evidence:

1. The process exited successfully, the run's `.xml` contains the selected test, and it passed with no failures or unexplained skips. A zero exit code alone is insufficient.
2. `.valtrion-results/<run-id>/result.json` says `status: PASS`, identifies `example.editor.smoke`, records `target.evidence: editor`, and contains the expected idle, wait and mark steps. Confirm `smoke.complete` in the captured log/step evidence. Validate the result with your installed `valtrion validate` command.
3. Read the run's `.log` for import/compilation errors. The smoke is a wiring check; a result may list unknown candidate metadata under `unverified` because this direct test does not supply a build candidate.

After Unity resolves the package, run `valtrion doctor --project <absolute-project-path> --json`. Inspect the `project` object: its `status` must be `ok` with Unity package 0.11.2 and an installed Core version that [compat.json](../compat.json) lists for it. `skipped` does not confirm integration. Doctor also checks tools for other platforms; unrelated missing prerequisites need not prevent this Editor smoke.

The direct smoke can list unknown candidate/host metadata under `unverified`. With package 0.10.x in `testables` it can also list its intentionally malformed `test.badsignature` test fixture as an ignored registration warning (from 0.11.0 on, without `testables`, that fixture is not compiled in your project and the warning does not appear). Preserve those diagnostics. The passing XML, expected plan, all three passing steps and `smoke.complete` mark are still required; other warnings need investigation. This does not establish game behavior or device performance.

## Next steps

Add steps that exercise your app and assert its behavior. See the [Unity guide](../integrations/unity/com.goryned.valtrion.unity/Documentation~/index.md) for custom steps and the [providers guide](providers.md) for project build methods, the Editor provider's runner interface, device runs and profiles. The direct test above does not set up a provider runner or player build pipeline. Do not use the larger [example checks map](../examples/checks.json) unchanged: its build methods, runners and app scenarios are placeholders for a project's own integration.
