# Valtrion Unity

Unity is Valtrion's first and currently only ready application integration. Valtrion manages repeatable checks and their
execution evidence; Core also supervises ordinary processes without Unity. This package (0.16.0) ships the **contracts** (C# readers and writers for the Valtrion JSON documents) and the
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
MIT licensed; the notice is included in `LICENSE.md`. Core 0.19.0 and this package 0.16.0 are paired by `compat.json`; identical
version numbers are not required. The repository tests use Unity Test Framework 1.8.0; the repository's test Editor is specified in
`integrations/unity/UnityProject/ProjectSettings/ProjectVersion.txt`.

## Layout

| Path | Content |
| --- | --- |
| `Runtime/` | `Valtrion.Unity`: `Contracts/` (documents), `Json/` (value tree, parser, writer), `Harness/` (inbox, runner, steps, result writer, ledger) and `Probes/` |
| `Runtime/AssemblyInfo.cs` | `[assembly: AlwaysLinkAssembly]`: keeps `Valtrion.Unity` in IL2CPP players although nothing references it (see below) |
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
In asmdef references replace `GORYNED.Validation` by `Valtrion.Unity`; in a project `link.xml` that preserves the assembly replace the
`fullname` the same way (from 0.11.2 such an entry is no longer needed for the harness itself).

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
| `gdv.pipeline/1` | `PipelineDocument` | `candidate`, `device`, `stages[]` (`build`, `install`, `run`, `collect`; optional `externalUi`, `prelaunchFiles[]` and `cleanup[]`), `compare` |
| `gdv.result/1` | `ResultDocument` | `runId`, `status`, `reason`, `candidate`, `target`, `host`, `plan` (id + SHA-256), `layers`, `steps[]`, `metrics[]`, `comparison`, `checkout`, `build`, `cleanup`, `artifacts[]` and `unverified[]` (both required: an empty list is a claim, omission is not) |
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

## Test progress for the host

When the Test Framework package is installed, the Editor assembly `Valtrion.Unity.Editor.Progress` reports how far a Test Runner run has come. `valtrion unity run` sets the environment variable `GDV_PROGRESS_FILE`; the assembly then keeps that file up to date (`gdv.testprogress/1`: `total`, `done`, `passed`, `failed`, `skipped`, `current`, `state`), so the host's dashboard can show "120 of 361 tests" and the test that runs now. Without the variable it does nothing; it writes only that file and never changes a test or its result.

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
  Every plan the harness runs is consumed: the file, whatever source named it (`-gdv-plan` included), is renamed
  `<name>.done-<yyyyMMddTHHmmssZ>`. A second launch with the same `-gdv-plan` path therefore ends FAIL with `plan '<path>' given by
  -gdv-plan does not exist: an earlier run already consumed it (renamed to '<name>.done-<utc>')`; give every launch, for example a
  Mono and an IL2CPP player of the same project, its own fresh copy of the plan. A legacy file is read only through an `ILegacyPlanConverter`
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
| `probe.start` / `probe.stop` | `probes`: `"frameTiming"`, `"profiler"` (alias `"memory"`), `"timer:<metric>"`, `"log:<TAG>"` or objects (`{"type":"log","name","pattern","unit","metric"}`, `{"type":"logSpan","start","end","metric"}`: the milliseconds from a log line that matches `start` to the next one that matches `end`, one sample per pair, for what happens inside a step that cannot be split, such as the time a recording takes to finish after the stop request); `required` makes an unavailable probe BLOCKED, otherwise it is listed as unverified |
| `gc` | optional `unloadAssets` (default true) |
| `idle` | optional `frames` (default 1) |
| `screenshot` | `name`; writes `<name>.png` into the run folder. Without a graphics device the step ends `BLOCKED` at once |
| `log.expect` | `pattern`, optional `fromStart` |
| `file.put` | `path` (relative), `text`, optional `root` (`work`, `persistent`), `overwrite` |
| `app.quit` | optional `exitCode`; use as the last step |
| `external.ui` | `action`, optional `args`; writes the request into the heartbeat (state `waiting-host`) and waits for `inbox/reply-<id>.json`; refused inside an open probe window |
| `app.background` | `seconds` (greater than 0), optional `metric`. Asks the host to send the running app to the background, keep it there for `seconds` and bring it back: an `external.ui` request with `action` `app.background` and `args.seconds` (state `waiting-host`, reply `reply-<id>.json`); the host answers PASS when the app is in the foreground again, any other status ends the step with it. `timeoutSec` must cover the time in the background (the run clock keeps counting; a pipeline-hosted command is also capped by `externalUi.timeoutSec`, default 120). `seconds` is checked when the step runs. With `metric` the ms from the request to the reply are reported. Refused inside an open probe window. Valtrion supplies no host action: the pipeline's `externalUi.command` (or the caller's own host) implements `app.background`, see `docs/protocol.md`; what the app does while it is paused is for the project's own steps and conditions to check |
| `session.offsets` | `reader` (a name in `SessionReaders.Shared`; not registered is BLOCKED), optional `prefix` (default `session`), optional `relativeTo` (a stream name). Reports, without judging, the persisted offsets of the session just recorded as metrics: `<prefix>.<stream>.offsetMs`, `.firstTimestampMs`, `.firstTimestampFromAnchorMs` (with an anchor), `.offsetFromStreamMs` (with `relativeTo`) and `<prefix>.anchorMs` |
| `storage.begin` / `storage.end` | `reader` (a name in `StorageReaders.Shared`; not registered is BLOCKED), optional `prefix` (default `storage`), optional `required`; `storage.end` also takes optional `seconds`. Reports `<prefix>.freeBeforeBytes`, `.freeAfterBytes`, `.usedBytes` (before minus after) and `.bytesPerSecond` (used bytes over `seconds`, or over the time between the two steps). A reader that returns a negative number says "unknown": nothing depending on it is reported, it is listed as unverified, and with `required` the step is BLOCKED. Unknown is never read as zero |
| `timer.start` / `timer.stop` | `name`; `timer.stop` also takes `metric` and optional `until` (a condition of `ConditionRegistry`, waited for as `waitUntil` does). Reports the milliseconds from `timer.start` to the end of `timer.stop` as `metric`: for example the time from a stop request to a finished file. A timer that was not started is FAIL |
| `counter.start` / `counter.stop` | `name`; `counter.start` also takes `counters` (objects with `metric`, `pattern`, optional `mode` and `unit`) and optional `thermal` (a name in `ThermalReaders.Shared`; not registered is BLOCKED). Counts events in the log between the two steps: `mode` `count` (default) counts matching lines; `sum`, `last`, `max` and `increase` (last minus first, for a running counter that a statistics line repeats) read the number in the group `value` of `pattern`, for example `outOfBuffers=(?<value>\d+)`. One sample per counter and window; a count of nothing is a real 0, the other modes report nothing and list it as unverified when no line had a number. With `thermal` the metrics `<name>.thermalStartLevel` and `.thermalEndLevel` are reported next to the counters and the step message names both states (read at the two ends of the window, not per line). A window spans steps, so a window around one step gives a count per step and a window around all steps of the plan gives a count per run. `increase` needs two readings (one is unknown, not 0) and is listed as unverified when the number went down (a reset). A pattern that takes more than 50 ms on a line does not count that line and is listed as unverified. A metric that is unknown for an iteration (thermal state unknown) leaves that iteration out of its series, so series of different metrics are not paired by sample index: read the thermal levels of an iteration from the same result, not by position |
| `repeat` | `count` (at least 1), `steps` (a non-empty list of steps, each with `type` and `timeoutSec` as in a plan; `repeat` cannot be nested), optional `continueOnFail`, `prefix` (default `repeat`), `roundMetric`. Runs `steps` in order `count` times inside one step; its `timeoutSec` bounds the whole loop. The metrics of the inner steps get one sample per round (so the result has their n, median, p90 and max over the rounds), `<prefix>.rounds` and `<prefix>.failedRounds` are reported, and `roundMetric` gets the duration of every round in ms. A FAIL ends the round (the rest of it is skipped) and the loop, or goes on with the next round with `continueOnFail`; the step then ends FAIL with the number of failed rounds and the first failure. BLOCKED, NEEDS_PERMISSION or TIMEOUT of an inner step stops the loop with that status. The marks of the inner steps are not kept; their metrics and artifacts are. A project that registers its own step type `repeat` must rename it. Use it for arm-and-disarm series without recording. Unlike the plan's own `repeat`, it repeats a part of one iteration |
| `storage.fill` / `storage.release` | `storage.fill`: `reader` (a name in `StorageReaders.Shared`), `reserveBytes` (the floor that must stay free), exactly one of `bytes` (write that many) and `leaveFreeBytes` (write until that much is free; needs `toleranceBytes`), optional `chunkBytes` (default 16 MiB per frame) and `maxAdjustments` (default 10). `reserveBytes` is at least 1 and `toleranceBytes` goes with `leaveFreeBytes` only; none of the first three has a default. Occupies free space with a file of the run (`work/fill/gdv-fill-<run>.bin`), recorded in the cleanup ledger before it is created and removed when the run ends however it ends (a plan with `cleanup: "none"` included), after a 16-byte marker; the free space is read again after every write, so the step writes more when the system gave space back and cuts the file when it took some. BLOCKED, writing nothing, when the reader is not registered or says unknown (negative), when the target is below the floor, when the free space is already below the target, or when the fill folder holds a file this harness did not create (it is never touched). A reading below the floor cuts the file back to the floor at once and the step ends FAIL, as when the free space ends outside the tolerance or does not fall after three writes in a row (a reader that is stale or reads the wrong volume), or is unknown at the end. Reports `fill.bytesWritten` and `fill.freeAfterBytes`. `storage.release` removes the file now. A fill file that an earlier launch left (a killed app) is removed when the next run starts, not before: the device stays full until a plan is started again. Only a file directly in `work/fill`, named `gdv-fill-*.bin` and starting with the marker is removed; any other file is left alone and listed as unverified |
| `call.time` | `call` (a name in `ProjectCalls.Shared`; not registered is BLOCKED), `metric`. Runs the project call once and reports its duration in ms; a call that returns `ProjectCallResult.Unknown(note)` still has its time reported and the note is unverified; a call that throws is FAIL and reports nothing. Put it in `repeat` for a distribution |

**Session readers.** `session.offsets` reads a recorded session through code the project registers, because the session format is the
project's: `SessionReaders.Shared.Register("session", context => { var offsets = new SessionOffsets { AnchorMs = ... }; offsets.Streams.Add(new StreamOffset("video", offsetMs, firstTimestampMs)); return offsets; })`.
Return what the session stores (its own reader class or its file), not a recomputed value. Valtrion applies no threshold: a plan budgets
the metrics it cares about in its `metrics` section.

**Thermal readers.** `ThermalReaders.Shared.Register("device", context => new ThermalReading(level, label))` gives `counter.start` and `counter.stop` the thermal state of the device; return null when it is unknown. The reader is called on the main thread. Valtrion does not interpret the level and applies no threshold.

Probes: `FrameTimingProbe`, `ProfilerRecorderProbe` (`gc.allocBytes`, `mainThread.ms`, `memory.totalUsedMB`,
`memory.systemUsedMB`), `TimerProbe`, `LogTagProbe`, `LogSpanProbe`. A project adds one with `ProbeFactory.Register`.

**Adding steps.** A project writes `[ValidationStep("app.openMenu")] static IEnumerator OpenMenu(StepContext context)`; the registry
finds it by attribute in the loaded non-engine assemblies. In IL2CPP the method must survive stripping: preserve its assembly in a
`link.xml` of the project (under `Assets/`; UnityLinker does not read a `link.xml` inside a package), add
`[UnityEngine.Scripting.Preserve]`, or skip reflection and call `StepRegistry.Register` from your own bootstrap.

**IL2CPP stripping of the harness itself.** No project code references `Valtrion.Unity`: the harness starts from its own
`RuntimeInitializeOnLoadMethod`. UnityLinker removes an unreferenced assembly, and a `link.xml` shipped inside a package is not
read (Unity manual, managed code stripping), so up to 0.11.1 an IL2CPP development player lost the harness unless the project
preserved `Valtrion.Unity` in its own `Assets/link.xml`. From 0.11.2 the assembly is marked `[assembly: AlwaysLinkAssembly]`
(`Runtime/AssemblyInfo.cs`): the linker always processes it and keeps the bootstrap and everything it reaches, the built-in steps
and probes included. No project `link.xml` is needed for the harness; an existing one that preserves `Valtrion.Unity` does no harm.

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
