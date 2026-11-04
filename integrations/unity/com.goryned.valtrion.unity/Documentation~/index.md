# Valtrion Unity

Unity is Valtrion's first and currently only ready application integration. Valtrion manages repeatable checks and their
execution evidence; Core also supervises ordinary processes without Unity. This package (0.11.0) ships the **contracts** (C# readers and writers for the Valtrion JSON documents) and the
development-only Runtime **harness** (plan inbox, runner, steps, probes, result writer). The host CLI, the JSON Schemas and the
golden fixtures live in the repository root (`core/` and `contracts/`); see the repository `docs/` for the CLI.

## Package rules

- **No dependency** on any other package except the built-in modules `imageconversion` and `screencapture` (used by the
  `screenshot` step). Any Unity project can take it alone.
- **Development only.** `Valtrion.Unity` (Runtime) is constrained by `DEVELOPMENT_BUILD || UNITY_EDITOR`; a release player
  contains no type of it. The assembly is auto-referenced, so a project's `Assembly-CSharp` can hold validation steps; that code
  must sit under `#if DEVELOPMENT_BUILD || UNITY_EDITOR`, because in a Release build the assembly does not exist. The Editor
  assembly references only the Runtime assembly; the plan test helper `Valtrion.Unity.PlayModeTests` references the Runtime
  assembly and, only when the project has `com.unity.test-framework`, NUnit and `UnityEngine.TestRunner` (it is not a package
  dependency: without the Test Framework the helper is simply not compiled).
- **JSON** in the Runtime uses `Valtrion.Unity.Json.JsonValue`, a small reader and writer with no reflection and no external
  package, so it is safe for IL2CPP and stripping. Free-form parts (step arguments, event data) stay value trees.
- **Wire identifiers are stable.** Schema ids (`gdv.plan/1`, `gdv.result/1`, ...), step type names, launch arguments (`-gdv-plan`,
  `-gdv-run-id`, `-gdv-out`), environment variables (`GDV_PLAN`, `GDV_RUN_ID`, `GDV_OUT_ROOT`), the intent extras (`gdv-plan`,
  `gdv-run-id`, `gdv-out`) and the log line `GDV-RESULT` keep their names: baselines are keyed by the SHA-256 of the plan file,
  so renaming them would invalidate existing plans and baselines.

## Install

Start with the repository's [Unity Quick Start](../../../../docs/unity-quick-start.md) for a complete Editor smoke test. Install from the
same reviewed checkout as Core. Add a local path to `Packages/manifest.json` for a trial (replace the example path):

```json
"com.goryned.valtrion.unity": "file:/absolute/path/to/Valtrion/integrations/unity/com.goryned.valtrion.unity"
```

For a shared project, use `https://github.com/GORYNED/Valtrion.git?path=integrations/unity/com.goryned.valtrion.unity#<full-reviewed-commit-sha>`
with the full SHA substituted; do not commit an absolute local path. Public HTTPS Git installation needs no GitHub account or token; private forks require existing access. This package is
MIT licensed; the notice is included in `LICENSE.md`. Core 0.13.1 and this package 0.11.0 are paired by `compat.json`; identical
version numbers are not required. The repository tests use Unity Test Framework 1.8.0; the repository's test Editor is specified in
`integrations/unity/UnityProject/ProjectSettings/ProjectVersion.txt`.

## Layout

| Path | Content |
| --- | --- |
| `Runtime/` | `Valtrion.Unity`: `Contracts/` (documents), `Json/` (value tree, parser, writer), `Harness/` (inbox, runner, steps, result writer, ledger) and `Probes/` |
| `Runtime/link.xml` | preserves the `Valtrion.Unity` assembly from IL2CPP stripping |
| `Editor/` | `Valtrion.Unity.Editor`: `PlanTestBuildAssemblyFilter`, which keeps the plan test helper out of player builds that do not include test assemblies |
| `Testing/` | `Valtrion.Unity.PlayModeTests` (namespace `Valtrion.Unity.Testing`): `PlanTest.Run`, `NUnitXmlResultWriter`; an ordinary package assembly, no `testables` needed |

The package contains no tests of its own. Valtrion's tests live in the repository verification project
`integrations/unity/UnityProject` (`Assets/Tests/`), which references this package with `file:` and has no `testables` entry:
`Valtrion.Unity.Tests` (EditMode: round trip of every valid fixture, rejection of every invalid one, plan inbox and runner) and
`Valtrion.Unity.HarnessPlayModeTests` (PlayMode: harness on the main thread, `PlanTest.Run`, NUnit XML writer). The contract tests
read the golden fixtures from `contracts/fixtures/valid` and `contracts/fixtures/invalid` of the repository checkout above the
package folder; without one they end as Ignored with a message instead of failing.

## Renamed from the previous validation package

This package continues `com.goryned.unity.validation` 0.6.9. Consumers update as follows:

| Before | After |
| --- | --- |
| package `com.goryned.unity.validation` | `com.goryned.valtrion.unity` |
| assembly `GORYNED.Validation` | `Valtrion.Unity` |
| assembly `GORYNED.Validation.Editor` | `Valtrion.Unity.Editor` |
| assembly `GORYNED.Validation.Tests` | `Valtrion.Unity.Tests` |
| assembly `GORYNED.Validation.PlayModeTests` | `Valtrion.Unity.PlayModeTests` |
| namespaces `GORYNED.Validation[.Json\|.Contracts\|.Harness\|.Probes]` | `Valtrion.Unity[.Json\|.Contracts\|.Harness\|.Probes]` |

Class names (`ValidationHarness`, `PlanTest`, `ValidationStepAttribute`, `PlanInbox`, ...) and all wire identifiers are unchanged.
In asmdef references replace `GORYNED.Validation` by `Valtrion.Unity`; in a `link.xml` that preserves the assembly replace the
`fullname` the same way.

## Statuses, evidence and exit codes

| Status | Meaning | Exit code |
| --- | --- | --- |
| `PASS` | all checks passed | 0 |
| `FAIL` | a check failed, or a metric regressed | 1 |
| `BLOCKED` | a prerequisite is missing; nothing was proven | 2 |
| `NEEDS_PERMISSION` | the owner must act (unlock, trust, grant); the text says what | 2 |
| `STALL` | no progress for the stall window | 3 |
| `TIMEOUT` | the time budget was exceeded | 3 |

Per-layer outcomes in a result use the same statuses; a layer that did not run is absent or `BLOCKED`, never inferred from a
neighbour. Evidence says what produced a result: `physical` (a real device), `emulator`, `editor` (the Unity Editor; never
device evidence) or `host` (the build or host machine only). Only `physical` evidence supports a device performance claim. A
result with `BLOCKED` or `NEEDS_PERMISSION` is never reported as PASS.

## Contracts

Every document has a `schema` property with its id. A breaking change adds `/2`; additive fields in open objects do not. Closed
objects reject unknown properties.

| Schema | C# type | Purpose |
| --- | --- | --- |
| `gdv.plan/1` | `PlanDocument` | `id`, `kind` (`functional`, `performance`, `soak`, `smoke`), `requires`, `dataPolicy` (`isolated`, `devdata`, `clean-install`), `budgetSeconds`, `repeat`, `warmup`, `continueOnFail`, `setup[]`, `steps[]` (each `type` + mandatory `timeoutSec` + free-form arguments), `metrics{}`, `cleanup` |
| `gdv.pipeline/1` | `PipelineDocument` | `candidate`, `device`, `stages[]` (`build`, `install`, `run`, `collect`; optional `prelaunchFiles[]`), `compare` |
| `gdv.result/1` | `ResultDocument` | `runId`, `status`, `reason`, `candidate`, `target`, `host`, `plan` (id + SHA-256), `layers`, `steps[]`, `metrics[]`, `comparison`, `artifacts[]` and `unverified[]` (both required: an empty list is a claim, omission is not) |
| `gdv.heartbeat/1` | `HeartbeatDocument` | rewritten atomically by the Runtime every few seconds: `seq`, `state`, current step, `elapsedMs`, optional host `request` |
| `gdv.job/1` | `JobDocument` | state of a detached host job |
| `gdv.event/1` | `EventDocument` | one line of `events.jsonl` |
| `gdv.hosts/1` | `HostsDocument` | machine-local host inventory |
| `gdv.registry/1` | `RegistryDocument` | devices with class, capabilities, exclusivity |
| `gdv.lease/1` | `LeaseDocument` | owner, session, pid, host, TTL, renewal |
| `gdv.checks/1` | `ChecksDocument` | path globs to kinds; kinds to checks per level |
| `gdv.baseline/1` | `BaselineDocument` | summary stats per metric under a comparability key; editor evidence cannot be a baseline |

**Comparability key.** Two results are compared only when plan SHA-256, device class, OS major version, build profile and Unity
version are all equal. Otherwise the verdict is `NOT_COMPARABLE`, never `PASS` (`ComparabilityKey.IsComparableWith`).

**Rules beyond the schemas.** The C# classes enforce, as the CLI does: an `external.ui` step must not run between `probe.start`
and `probe.stop` in a `performance` plan; a PASS result has no `reason` and no layer that is not PASS, any other status carries a
`reason`; host ids are unique, at most one host is `local`, an `ssh` host names its target and a `local` host has none, and a
target never starts with `-`. Parsing is strict: no comments, trailing commas, duplicate property names, NaN or infinity, nesting
beyond 64 levels; numbers outside about 2^53 are not integers, and counts, seconds and process ids are capped at 2147483647. A
text file is read as UTF-8 with one optional byte-order mark; the C# writer escapes U+0085, U+2028 and U+2029 so a line of
`events.jsonl` never breaks inside a string.

**Fixtures.** `contracts/fixtures/valid` has one document per schema; `contracts/fixtures/invalid` has files named
`<schema-name>-<reason>.json` that both the CLI validator and the C# contracts reject.

## C# interfaces

```csharp
namespace Valtrion.Unity.Json {
  enum JsonKind { Null, Bool, Number, String, Array, Object }
  sealed class JsonValue { static JsonValue Parse(string); string Write(bool indented); bool DeepEquals(JsonValue);
                           static JsonValue From(bool|double|string); NewArray(); NewObject(); Add(...); Set(name, value); TryGet(name, out v);
                           JsonKind Kind; AsBool; AsNumber; AsString; Items; Members; Count }
  sealed class JsonException : Exception   // malformed text, with offset
}
namespace Valtrion.Unity.Contracts {
  interface IGdvDocument { string Schema { get; } JsonValue ToJson(); }
  sealed class PlanDocument / PipelineDocument / ResultDocument / HeartbeatDocument / JobDocument / EventDocument /
               HostsDocument / RegistryDocument / LeaseDocument / ChecksDocument / BaselineDocument   // : IGdvDocument
        { static T Parse(string); static T FromJson(JsonValue); JsonValue ToJson(); string ToJsonString(); }
  static class ContractSerializer { static IGdvDocument Parse(string); static IGdvDocument FromJson(JsonValue); }
  sealed class ContractException : Exception       // names the JSON path
  static class ValidationStatus { Pass, Fail, Blocked, NeedsPermission, Stall, Timeout; All; int ExitCode(string) }
  static class EvidenceKind { Physical, Emulator, Editor, Host }
  static class SchemaIds { Plan ... Baseline; All }
  sealed class ComparabilityKey { bool IsComparableWith(ComparabilityKey) }
}
```

## Runtime harness

Nothing runs unless a plan is found, and the bootstrap never runs inside a Unity Test Framework play session (detected by the
runner's `PlaymodeTestsController` object, by `-runTests` on the command line, by `ValidationHarness.TestOwned`, or as a fallback
by its scene `InitTestScene…`): a test starts its plan itself, with `PlanTest.Run(path)` or `PlanTest.RunFromEnvironment()`, so
one plan never runs twice (`ValidationHarness.BootstrapDecision` records what the bootstrap did: `NoPlan`,
`SuppressedByTestRun`, `Started`). The bootstrap (`RuntimeInitializeOnLoadMethod`, after the first scene) checks the plan sources
and returns. A Release player contains no type of the assembly. Start a run by hand with `ValidationHarness.Start(plan, text, options)`.

- **Inputs** (plan, run id, output folder), in precedence: launch arguments `-gdv-plan <path>`, `-gdv-run-id <id>`,
  `-gdv-out <dir>`; environment variables `GDV_PLAN`, `GDV_RUN_ID`, `GDV_OUT_ROOT`; Android intent extras `gdv-plan`, `gdv-run-id`,
  `gdv-out`; then, for the plan only, the inbox file `<persistentDataPath>/gdv/inbox/plan.json`. `-gdv-out`/`GDV_OUT_ROOT`/`gdv-out`
  replaces `<persistentDataPath>/gdv/out`: heartbeat, result, artifacts and the NUnit XML go to `<out>/<runId>/`. An explicit plan
  path that names no file ends the run as FAIL; it never falls back to another source. Without a run id it is `<plan id>-<utc>-<4 hex>`.
  A consumed inbox file is renamed `<name>.done-<yyyyMMddTHHmmssZ>`. A legacy file is read only through an `ILegacyPlanConverter`
  that the project adds to `PlanInbox.LegacyConverters`. A plan that fails the contract ends the run as FAIL with the reason.
- **Run:** `setup` once, then `warmup` + `repeat` iterations of `steps`. Warm-up iterations run but contribute no metric samples and
  no recorded passing steps. Every step has its `timeoutSec` and the plan its `budgetSeconds`; the first non-PASS step ends the run
  unless `continueOnFail` (the first failure is the result status; TIMEOUT of the budget always wins). An unknown step type is
  BLOCKED. A step that throws fails with the exception type, its message and the first stack frame, and the full exception goes to
  the log as `GDV: step <step> threw ...`. The runner is a coroutine on the main thread; a step handler yields only `null` or
  `WaitForEndOfFrame` and never blocks. The host's per-frame `Heartbeat()` ends a step that is still running one second after its
  timeout as TIMEOUT. `cleanup: "ledger"` (the default) undoes only what the run recorded in its `CleanupLedger` (`file.put` records
  its files; a project adds `ICleanupHandler`s through `RunOptions.CleanupHandlers`).
- **Files** in `<persistentDataPath>/gdv/`: `inbox/` (plan, optional `candidate.json`, `reply-<id>.json`), `out/<runId>/result.json`
  (`gdv.result/1`, atomic), `out/<runId>/heartbeat.json` (every 2 s and at every step), `ledger.json`, artifacts, `out/current.json`
  and `work/` (`file.put` base). The terminal log line is `GDV-RESULT <STATUS> run=<id>`, exactly once; the app stays open unless
  a step `app.quit` asked for the exit. Without `candidate.json` the commit is unknown (zeros) and the result lists that as
  unverified. Editor runs report `editor` evidence.
- **Statistics:** `n`, median, nearest-rank p90, max, stdev as in `gdv.result/1`. The frame and main-thread probes also report
  `<metric>.p99` as its own metric, which plans can budget (`frameTime.deltaMs.p99`).

| Step | Arguments |
| --- | --- |
| `wait` | `seconds` |
| `waitUntil` | `condition` (a name in `ConditionRegistry.Shared`; not registered is BLOCKED) |
| `mark` | `name`, optional `metric`; records ms since the iteration start |
| `probe.start` / `probe.stop` | `probes`: `"frameTiming"`, `"profiler"` (alias `"memory"`), `"timer:<metric>"`, `"log:<TAG>"` or objects (`{"type":"log","name","pattern","unit","metric"}`); `required` makes an unavailable probe BLOCKED, otherwise it is listed as unverified |
| `gc` | optional `unloadAssets` (default true) |
| `idle` | optional `frames` (default 1) |
| `screenshot` | `name`; writes `<name>.png` into the run folder. Without a graphics device the step ends `BLOCKED` at once |
| `log.expect` | `pattern`, optional `fromStart` |
| `file.put` | `path` (relative), `text`, optional `root` (`work`, `persistent`), `overwrite` |
| `app.quit` | optional `exitCode`; use as the last step |
| `external.ui` | `action`, optional `args`; writes the request into the heartbeat (state `waiting-host`) and waits for `inbox/reply-<id>.json`; refused inside an open probe window |

Probes: `FrameTimingProbe`, `ProfilerRecorderProbe` (`gc.allocBytes`, `mainThread.ms`, `memory.totalUsedMB`,
`memory.systemUsedMB`), `TimerProbe`, `LogTagProbe`. A project adds one with `ProbeFactory.Register`.

**Adding steps.** A project writes `[ValidationStep("app.openMenu")] static IEnumerator OpenMenu(StepContext context)`; the registry
finds it by attribute in the loaded non-engine assemblies. In IL2CPP the method must survive stripping: preserve its assembly in a
`link.xml` (this package's `Runtime/link.xml` preserves `Valtrion.Unity` itself), add `[UnityEngine.Scripting.Preserve]`, or skip
reflection and call `StepRegistry.Register` from your own bootstrap.

**Tests in the Unity Test Framework.** `Testing/` (`Valtrion.Unity.PlayModeTests`) holds `PlanTest.Run(planPath)` for
`[UnityTest]` (Editor PlayMode and player test builds; fails unless PASS) and `NUnitXmlResultWriter`, a `TestRunCallback` that
writes `nunit-results.xml` into the run folder of the last plan test. A project test assembly references that assembly by name
(it is not auto-referenced), and the profile's `editor.planAssembly` names the project's test assembly, not this one.

**No `testables` entry is needed (package 0.11.0 and later).** `Valtrion.Unity.PlayModeTests` is an ordinary package assembly,
compiled whenever the project has `com.unity.test-framework`; the package's own tests are not part of the package, so a consumer's
test runs contain only the consumer's tests. In player builds the helper follows the Test Framework's rule for
`UnityEngine.TestRunner` and NUnit: `PlanTestBuildAssemblyFilter` (Editor) removes `Valtrion.Unity.PlayModeTests.dll` unless the
build includes test assemblies or `PlayerSettings.playModeTestRunnerEnabled` is on, so an ordinary development player (device runs
with `-gdv-plan`) does not contain it and a PlayMode test player does. A consumer manifest needs only:

```json
{
  "dependencies": {
    "com.goryned.valtrion.unity": "file:../path/to/Valtrion/integrations/unity/com.goryned.valtrion.unity",
    "com.unity.test-framework": "1.8.0"
  }
}
```

**Upgrading from 0.10.x or earlier.** Up to 0.10.x the helper was a test assembly that Unity compiled only with the package in
`testables`, which also brought the package's own tests (and Ignored contract-fixture tests) into the consumer's runs. After the
upgrade remove `com.goryned.valtrion.unity` from `testables` (and the `testables` key if it held nothing else), and drop any
exclusion of `Valtrion.Unity.Tests` classes such as `ContractFixtureTests` from the project's test runs. Asmdef references to
`Valtrion.Unity.PlayModeTests`, the namespace `Valtrion.Unity.Testing` and `editor.planAssembly` stay as they are. A stale
`testables` entry is harmless: the package has no test assemblies left to compile.
