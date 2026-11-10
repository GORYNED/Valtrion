# Changelog

## 0.17.0 (2026-11-09)

- Compatible Core versions are listed in `compat.json`: package 0.17.0 works with Core 0.8.0 up to, not including, 0.21. Packages 0.15.x and 0.16.x are paired with Core below 0.20. The Editor tests of the contract fixtures (100) pass with the change below; update the package tag in `Packages/manifest.json` to `unity-v0.17.0`.

- **`checks.json`: `byContent.assemblies` (#131).** The map reader accepts the new optional member of `byContent` (`kind`, `runner`, `playmodeRunner`, `level`) that Core 0.20.0 uses to narrow a package pin change to the test assemblies that depend on what changed, and writes it back. Contracts and steps are otherwise unchanged. A map without the member reads as before.

## 0.16.2 (2026-11-09)

- Compatible Core versions are listed in `compat.json`: package 0.16.2 works with Core 0.8.0 up to, not including, 0.20 (the pairing of 0.16.x is unchanged). Contracts and every step other than `storage.fill` are unchanged. The Editor tests (compilation and EditMode) pass with the change below; update the package tag in `Packages/manifest.json` to `unity-v0.16.2`.

- **`storage.fill`: optional `staleBytes` (#113).** The guard that stops the fill when the free-space reading does not fall (a stale or wrong reader) counted three writes in a row. On a device whose reading includes space the system can free again, the reading stays on a plateau while that space is reclaimed, and the guard stopped a correct fill. `staleBytes` gives the plateau in bytes: the fill goes on while the amount written since the reading last fell is not more than that. Without it nothing changes (three writes in a row). The floor still cuts the file back at once whenever a reading is below it.

## 0.16.1 (2026-11-09)

- Compatible Core versions are listed in `compat.json`: package 0.16.1 works with Core 0.8.0 up to, not including, 0.20 (the pairing of 0.16.x is unchanged). Contracts and every step other than `storage.fill` are unchanged. The Editor tests (compilation and EditMode) pass with the change below; update the package tag in `Packages/manifest.json` to `unity-v0.16.1`.

- **`storage.fill`: `reserveBytes` may be left out with `leaveFreeBytes` (#113).** The floor is then `leaveFreeBytes` minus `toleranceBytes` (at least 1), the lowest free space the tolerance admits, so a plan that states the target and the tolerance needs no third number; a reading below the floor still cuts the file back at once. With `bytes`, `reserveBytes` stays required, and an explicit `reserveBytes` is used as before.

## 0.16.0 (2026-11-09)

- Compatible Core versions are listed in `compat.json`: package 0.16.0 works with Core 0.8.0 up to, not including, 0.20 (package 0.15.x now also up to, not including, 0.20). The package adds the plan steps `session.offsets`, `storage.begin`, `storage.end`, `timer.start`, `timer.stop`, `counter.start`, `counter.stop`, `repeat`, `storage.fill`, `storage.release`, `call.time` and `app.background` and the probe `logSpan` (#113); a plan that uses them needs this package, and the shape rules of `repeat` and `app.background` in `validate` need Core 0.19.0. The Editor tests (compilation and EditMode, 277 tests) pass with the changes below; update the package tag in `Packages/manifest.json` to `unity-v0.16.0`. Nothing changes for a plan that uses none of them. None of the steps applies a threshold or has a default norm.

- **`app.background` step (#113).** Asks the host to send the running app to the background for `seconds` and bring it back: a request `app.background` (`args.seconds`) through the same heartbeat and reply files as `external.ui`, so it is refused inside a probe window and its `timeoutSec` must cover the time away. With `metric` the time from the request to the reply is reported. Valtrion supplies no host action; the pipeline's `externalUi.command` implements it. `PlanDocument` rejects `app.background` between `probe.start` and `probe.stop` of a performance plan, like `external.ui`.
- **`storage.fill`, `storage.release` and `call.time` steps (#113).** `storage.fill` occupies free space of the device with a file of the run, to test the app on an almost full device: the floor that must stay free (`reserveBytes`), the target and the tolerance are arguments without defaults; it refuses to start when free space is unknown or the target is below the floor, never touches a file it did not create, reads the free space again after every write, records the file in the cleanup ledger before the first byte, the file is removed at the end of the run whatever the plan's `cleanup` says, a reading below the floor cuts it back at once, a reader that does not show the free space falling is not trusted, and a file left by a killed app (marked, in the fill folder) is removed when the next run starts. `call.time` reports the duration of a project call registered with `ProjectCalls.Shared.Register` (for example a free-space read), unknown answers are listed as unverified. `CleanupLedger.Forget` takes an entry out that its step already undid. No threshold is applied.
- **`repeat` step (#113).** Runs a list of steps `count` times inside one step (`steps`, `continueOnFail`, `roundMetric`), so a plan can arm and disarm without recording N times and still get a summary: one sample of every inner metric per round, `repeat.rounds`, `repeat.failedRounds`, and the first failure in the step message. `RunContext.Steps` gives a step the registry of the run, and `StepContext.Round` names the round; a screenshot taken in a round has the round in its file name. `repeat` is now a built-in step type: a project that registered its own `repeat` step gets a duplicate-registration problem and must rename it. No threshold is applied.
- **`counter.start` and `counter.stop` steps (#113).** Count events in the log between two points of a run (dropped frames, faulted phases, out-of-buffers) as metrics, so their frequency can be compared between runs: matching lines, or the sum, last, max or increase of a number read from the line. With a thermal reader the project registers (`ThermalReaders.Shared.Register`), the thermal state at the start and at the end of the window is reported next to the numbers. No threshold is applied.
- **`storage.begin`, `storage.end`, `timer.start`, `timer.stop` steps (#113).** Report the free space of the device before and after a recording step, the bytes used and written per second, and the time from a start stamp to the end of a condition (the time a recording takes to finish after the stop request) as metrics. The free space comes from a reader the project registers with `StorageReaders.Shared.Register`; a negative value means unknown and is never read as zero. `RunContext.State` lets steps of one run hand state to each other. No threshold is applied. The probe `{"type":"logSpan","start":"...","end":"...","metric":"..."}` measures the milliseconds between two log lines inside a probe window, for a span that happens inside one step.
- **`session.offsets` step (#113).** Reports the persisted per-stream start offsets and first timestamps of the session just recorded as metrics (`<prefix>.<stream>.offsetMs`, `.firstTimestampMs`, `.firstTimestampFromAnchorMs`, `.offsetFromStreamMs`, `<prefix>.anchorMs`). The project registers its own reader with `SessionReaders.Shared.Register`; Valtrion reads no session format and applies no threshold.

## 0.15.0 (2026-11-08)

- Compatible Core versions are listed in `compat.json`: package 0.15.0 works with Core 0.8.0 up to, not including, 0.19. A project that uses `extends`, `bindings`, `overridesPreset`, `allowHeavyFallback` or `byContent` in its `checks.json` (Core 0.18.0) needs this package to open the map in Unity; update the package tag in `Packages/manifest.json` to `unity-v0.15.0`. Packages 0.14.x and older are paired with Core below 0.18. The Editor tests (compilation and EditMode) pass with the changes below.

- `ChecksJsonKey.ByContent` (`ChecksByContent` with `Managed` and `Platform`): the optional `byContent` of a `jsonRules` key of `gdv.checks/1` (Core, #106) is read and written; a missing member is rejected. Documents without it behave as before.

- `ChecksDocument.Extends` and `ChecksDocument.Bindings` (slot name to a list of check entries), `ChecksArea.OverridesPreset`, and `ContractReader.StringListMap`: the optional `extends`, `bindings` and `overridesPreset` of `gdv.checks/1` (Core, #105) are read and written. With `extends`, `Areas` and `Kinds` may be null (absent in the document); without it both are required as before. A malformed preset id is rejected.

- `ChecksJsonKey.AllowHeavyFallback`: the optional `allowHeavyFallback` boolean of a `jsonRules` key of `gdv.checks/1` (Core, #104) is read and written. Documents without it behave as before.

## 0.14.0 (2026-11-08)

- `ChecksDocument.EquivalenceRules` (`ChecksEquivalenceRule` with `Command` and `TimeoutSec`): the optional `equivalenceRules` key of `gdv.checks/1` (Core 0.17.0, #95) is read and written. Documents without it behave as before. Before, a `checks.json` that uses the key was rejected with `unexpected property`. The Editor tests (compilation and EditMode) pass with the change.
- Compatible Core versions are listed in `compat.json`: package 0.14.0 works with Core 0.8.0 up to, not including, 0.18. A project that uses `equivalenceRules` needs this package and Core 0.17.0 or newer; update the package tag in `Packages/manifest.json` to `unity-v0.14.0`.

## 0.13.0 (2026-11-06)

- **Test windows stay off the screen on macOS (Editor).** A new Editor assembly `Valtrion.Unity.Editor.WindowIsolation` (macOS Editor only, no other dependency) acts in a batch-mode Editor when the environment variable `GDV_ISOLATE_WINDOWS=1` is set (an interactive Editor ignores it with a warning), which `valtrion unity run` does on macOS by default (Core with the same change). The Editor process then may not become the active application, and every window it opens (`EditorWindow`, utility window, popup) is made transparent and ignores the mouse before it is shown; the tests that open them run as before. Without the variable nothing changes. The hook is removed again before a domain reload.
- Contracts are unchanged. Compatible Core versions are listed in `compat.json`: package 0.13.0 works with Core 0.8.0 up to, not including, 0.17 (the window isolation is switched on by Core 0.16.0 or newer; with an older Core the assembly does nothing). Packages 0.12.x are paired with Core below 0.16: update the package to 0.13.

## 0.12.0 (2026-11-05)

- **Test progress for the host (Editor).** A new Editor assembly `Valtrion.Unity.Editor.Progress` (compiled only when the Test Framework package is present, `UNITY_TESTS_FRAMEWORK`) registers Test Runner callbacks when the environment variable `GDV_PROGRESS_FILE` is set, which `valtrion unity run` does. It keeps that file up to date (`gdv.testprogress/1`: `total`, `done`, `passed`, `failed`, `skipped`, `current`, `state`) so the host can show "120 of 361 tests" instead of a moving bar. While a run goes the file is rewritten every 2 s from a background thread (`utc`: the Editor process is alive), and `mainUtc` says when the main thread last ran, so the host can tell a long test (the main thread is busy with it) from an Editor that has stopped writing. Without the variable it does nothing; it only writes that file and never changes a test or its result. Contracts and the runtime harness are unchanged.
- Compatible Core versions are listed in `compat.json`: package 0.12.0 works with Core 0.8.0 up to, not including, 0.16 (the progress report needs Core 0.15.0 or newer to be shown; with an older Core it is simply not read). Packages 0.11.x are paired with Core below 0.15: update the package to 0.12.0 when you update Core to 0.15.0.

## 0.11.2 (2026-11-05)

- **The harness survives IL2CPP stripping without a project `link.xml` (#80).** Nothing in a project references `Valtrion.Unity` (the harness starts from its own `RuntimeInitializeOnLoadMethod`), so UnityLinker removed the whole assembly from IL2CPP development players and a launch with `-gdv-plan` did nothing. `Runtime/link.xml` never prevented that: UnityLinker does not read a `link.xml` inside a package, and the earlier notes and the documentation that said it preserves `Valtrion.Unity` were wrong. The assembly is now marked `[assembly: UnityEngine.Scripting.AlwaysLinkAssembly]` (`Runtime/AssemblyInfo.cs`); the linker always processes it and keeps the bootstrap and what it reaches. The ineffective `Runtime/link.xml` is removed. A project `Assets/link.xml` that preserves `Valtrion.Unity` is no longer needed for the harness and may stay. Your own `[ValidationStep]` methods found by reflection still need a project `link.xml`, `[Preserve]` or `StepRegistry.Register`.
- **A consumed `-gdv-plan` is reported as consumed (#80).** The harness renames every plan it runs to `<name>.done-<utc>`, the one given by `-gdv-plan` included. (A consumed `GDV_PLAN` or intent extra was and stays ignored, because both outlive their plan; the hint applies to the launch argument. A bare relative name such as `-gdv-plan smoke.plan.json` is looked up in the working directory, like the plan itself.) A second player started with the same path (in #80: an IL2CPP player after a Mono player of the same project) answered only `plan '<path>' given by -gdv-plan does not exist`, which read like a path the IL2CPP player could not resolve. The message now continues `: an earlier run already consumed it (renamed to '<name>.done-<utc>'); give every launch a fresh copy of the plan`. The run still ends FAIL; nothing else changes. Path handling was not the cause: Windows IL2CPP development players ran plans given by a path with spaces and non-ASCII characters (`план Ωβ dir/smoke план.json`) and by a bare relative name, and a Mono player ran a path with spaces.
- Contracts are unchanged. Compatible Core versions are listed in `compat.json`: package 0.11.2 works with Core 0.8.0 up to, not including, 0.15.

## 0.11.1 (2026-11-04)

- **The contract readers accept every key Core 0.13.x and 0.14.0 write.** Before, these documents were rejected with `unexpected property`:
  - `PipelineStage.ExternalUi` (`PipelineExternalUi`: `externalUi` of a run stage, from Core 0.13.0): `Command` (the argv list, at least one non-empty item, required) and `TimeoutSec` (at least 1, optional);
  - `ResultDocument.Checkout` (`ResultCheckout`: `checkout` of `gdv.result/1`, written when the job's build stage settled the project commit): `Folder`, `Sha`, `Switched`, `Fetched` (required), `Id`, `Created`, `PreviousSha` (optional);
  - `HostEntry.Python` and `HostEntry.ValtrionCommand` (`python`, `valtrionCommand` of a `gdv.hosts/1` host, recorded by `valtrion hosts add` and `valtrion hosts sync`).
- `JobDocument.Cleanup` and `ResultDocument.Cleanup` (`OutputCleanup` with `Base`, `Removed`, `Kept` and `Refused` of `CleanupPathReason`) and `PipelineStage.Cleanup`: the optional `cleanup` keys of `gdv.job/1`, `gdv.result/1` and the `gdv.pipeline/1` stage (Core: declared job outputs, #75) are read and written. Documents without them behave as before.
- `ResultDocument.PipelineRun` (`ResultPipelineRun` with `PipelineId`, `ResumedFrom`, `KeepGoing` and `Stages` of `ResultStageRecord`) and `ResultDocument.ReusedFrom` (`ResultReusedFrom`): the optional `pipelineRun` and `reusedFrom` keys of `gdv.result/1` that Core writes for stage selection and the result cache (Core 0.14.0, #82). The strict reader accepts them and writes them back; a result without them reads as before.
- All additions are optional keys and the objects stay closed: an unknown key, a missing required key or a wrong type is still rejected, and documents without the new keys read and write as before.
- New golden fixtures `valid/result-checkout.json`, `valid/hosts-sync-paths.json`, `invalid/result-checkout-missing-fetched.json` and `invalid/hosts-empty-valtrion-command.json`; every valid fixture, `valid/pipeline-external-ui.json` included, now round-trips in `ContractFixtureTests`.
- The Runtime harness is unchanged. Compatible Core versions are listed in `compat.json`: package 0.11.1 works with Core 0.8.0 up to, not including, 0.15 (Core 0.14.0 included); 0.11.0 lacks these readers and stays limited to Core below 0.14.

## 0.11.0 (2026-11-04)

- **`PlanTest` and `NUnitXmlResultWriter` no longer need `testables` (#74).** The assembly `Valtrion.Unity.PlayModeTests` is now an ordinary package assembly instead of a test assembly. It moves from `Tests/PlayMode/` to `Testing/`; its name, namespace (`Valtrion.Unity.Testing`), public API and the GUIDs of its files are unchanged. It references NUnit and `UnityEngine.TestRunner` directly and is compiled whenever the project has `com.unity.test-framework` (any version; the same `versionDefines` rule the Test Framework uses for its own assemblies). Without the Test Framework it is left out and nothing fails to compile. It is still constrained by `DEVELOPMENT_BUILD || UNITY_EDITOR` and is not auto-referenced: a project test assembly references it by name as before. Players follow the Test Framework's own rule: the new Editor build filter `PlanTestBuildAssemblyFilter` (`IFilterBuildAssemblies`, in `Valtrion.Unity.Editor`) removes exactly `Valtrion.Unity.PlayModeTests.dll` from a player build unless the build includes test assemblies (`BuildOptions.IncludeTestAssemblies`) or `PlayerSettings.playModeTestRunnerEnabled` is on, the same condition under which the Test Framework strips `UnityEngine.TestRunner` and NUnit. An ordinary development player (the device mode with `-gdv-plan`) therefore does not contain the helper; a PlayMode test player does; a release player contains no type of it. `StepRegistry.Discover()` also skips the helper assembly (it holds no steps).
- **The package's own tests left the package.** `Valtrion.Unity.Tests` (including `ContractFixtureTests`, `PlanInboxTests`, `PlanRunnerTests`, `HarnessTestSupport`) and `HarnessPlayModeTests` now live in the repository verification project, `integrations/unity/UnityProject/Assets/Tests/`, which references the package with `file:` and no `testables` entry. The PlayMode tests are the assembly `Valtrion.Unity.HarnessPlayModeTests` (the test names `Valtrion.Unity.PlayModeTests.HarnessPlayModeTests.*` are unchanged). A consumer project no longer runs Valtrion's tests, and the nine `ContractFixtureTests` no longer end Ignored there; the `test.badsignature` registration diagnostic of `PlanInboxTests` no longer appears in a consumer's plan results.
- **Consumer manifest change** (any project that uses the package): remove `com.goryned.valtrion.unity` from `testables` in `Packages/manifest.json` (delete the whole `testables` key if it was the only entry), keep `com.unity.test-framework` in `dependencies`, and keep the asmdef reference to `Valtrion.Unity.PlayModeTests` and the profile's `editor.planAssembly` unchanged. Any exclusion of `Valtrion.Unity.Tests.ContractFixtureTests` (or of other `Valtrion.Unity.Tests` names) from the project's test runs can be removed. Leaving the package in `testables` still works, it just compiles nothing extra.
- Contracts and the Runtime harness are unchanged. Compatible Core versions are listed in `compat.json` (package 0.11.x: Core 0.8.0 up to, not including, 0.14).

## 0.10.0 (2026-11-03)

- `ChecksDocument.JsonRules` (`ChecksJsonRule`, `ChecksJsonKey` with `Kind` or `Kinds`): the optional `jsonRules` key of `gdv.checks/1` is read and written. Documents without it behave as before.
- Compatible Core versions are listed in `compat.json` (package 0.10.x: Core 0.8.0 up to, not including, 0.12).

## 0.9.0 (2026-11-03)

- `ChecksDocument.ContentRules` (`ChecksContentRule` with `Reindent` and `CodePrefixes`, `ChecksBlockComment`): the optional `contentRules` key of `gdv.checks/1` is read and written. Documents without it behave as before.

## 0.8.0 (2026-11-03)

- The contract readers and writers catch up with the schemas of Core 0.8.x and 0.9.0. All additions are optional keys, so documents written for 0.7.x still read and write unchanged:
  - `JobDocument.Phase` (`job.phase`: waiting-disk, waiting-build, building, waiting-device, on-device);
  - `RegistryDevice.Online` and `RegistryDevice.LastSeen` (`online`, `lastSeen` of a registry device);
  - `ResultDocument.Build` (`ResultBuild`: the build artifact a run was made with);
  - `ChecksDocument.Runners` (name to command template, non-empty values) and `ChecksArea.Exclude` (globs) of `gdv.checks/1`.
- Fixes the four failing EditMode tests of the repository project (job, registry and result fixtures, and the every-schema check): 112 of 112 pass.
- The Runtime harness is unchanged. Compatible Core versions are listed in `compat.json` (package 0.8.x: Core 0.8.0 up to, not including, 0.10).

## 0.7.1 (2026-10-30)

- First public release, tagged `unity-v0.7.1`: install with `https://github.com/GORYNED/Valtrion.git?path=integrations/unity/com.goryned.valtrion.unity#unity-v0.7.1`.
- Move the integration to `integrations/unity/com.goryned.valtrion.unity` and its verification project to `integrations/unity/UnityProject`. Update local and Git installation paths. Package identity, API, GUIDs and version are unchanged.
- Fix: the fixture-based contract tests (`ContractFixtureTests`) now end Ignored, not failed, when no `contracts/fixtures` folder exists above the package folder (a consumer project with the package in `testables` and no repository checkout). The repository verification project is now `integrations/unity/UnityProject`; see release readiness for current execution evidence.
- Document that `PlanTest` and `NUnitXmlResultWriter` live in the test assembly `Valtrion.Unity.PlayModeTests`, which Unity compiles only when the package is in the consumer's `testables`, with the manifest snippet.
- The Runtime assembly `Valtrion.Unity` is unchanged.

## 0.7.0 (2026-10-30)

Moved from the previous validation package `com.goryned.unity.validation` 0.6.9 into the Valtrion repository and renamed. Behaviour and the public API shape are unchanged apart from the names below; wire identifiers are unchanged.

- Package `com.goryned.unity.validation` is now `com.goryned.valtrion.unity` (display name `Valtrion Unity`).
- Assemblies: `GORYNED.Validation` -> `Valtrion.Unity`, `GORYNED.Validation.Editor` -> `Valtrion.Unity.Editor`, `GORYNED.Validation.Tests` -> `Valtrion.Unity.Tests`, `GORYNED.Validation.PlayModeTests` -> `Valtrion.Unity.PlayModeTests`. Namespaces `GORYNED.Validation[.X]` -> `Valtrion.Unity[.X]` (`Json`, `Contracts`, `Harness`, `Probes`). `Runtime/link.xml` was updated to the renamed assembly (correction, 0.11.2: a `link.xml` inside a package is not read by UnityLinker, so this file never preserved the assembly; see 0.11.2).
- Unchanged: schema ids (`gdv.*/1`), step type names, launch arguments (`-gdv-*`), environment variables (`GDV_*`) and the `GDV-RESULT` log line, so plan files, their SHA-256 and baselines stay valid.
- The host CLI and the schemas no longer ship inside the package (they live in `core/` and `contracts/` of the repository). The Editor contract tests read the golden fixtures from `contracts/fixtures` of the repository checkout.
- Add the test project (now `integrations/unity/UnityProject`).
- Documentation rewritten for Valtrion, with the rename mapping.

## Earlier history (previous validation package, Runtime and contracts only)

- 0.6.7: the C# `PipelineDocument` reads and writes `prelaunchFiles` on a stage and an absent `candidate.sha`. A step that throws fails with the exception type, its message and the first stack frame (`Faults`).
- 0.6.6: the harness honours every step's `timeoutSec`; the `screenshot` step ends `BLOCKED` at once when no graphics device renders frames, and the per-frame `Heartbeat()` ends a step still running one second after its timeout as `TIMEOUT`.
- 0.6.0: the development-only Runtime harness (`PlanInbox`, `PlanRunner`, `StepRegistry`, built-in steps, probes, `ResultWriter`, `CleanupLedger`), `PlanTest.Run` and the NUnit XML writer; the package declares the built-in modules `imageconversion` and `screencapture`.
- 0.1.0: the Runtime contracts and the reflection-free JSON reader and writer.
